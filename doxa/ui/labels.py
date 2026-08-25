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


# -- permission mode (v0.42.0; Claude Code's own palette, v0.50.0) -----
#
# The chip the operator asked for -- "in claude code you have an indicator
# to switch from manual to auto mode, we should adopt that" -- and then,
# in v0.50.0, asked to look like the thing it adopts: "the mode chip
# should have the same colors as it has in claude code and the same icon
# leading the mode label".
#
# EVERY glyph and colour below was READ OUT OF THE INSTALLED CLI, not
# guessed and not eyeballed from a screenshot. `claude` 2.1.228 is a
# bun-compiled ELF; its canonical permission-mode table survives in the
# bundle as a plain JS object literal keyed by the SAME mode names the SDK
# uses, which is what makes this a lookup rather than an interpretation:
#
#   ENc = {
#     default:           {title:"Manual",  symbol:Pin,      color:"inactive"},
#     plan:              {title:"Plan",    symbol:Pin,      color:"planMode"},
#     acceptEdits:       {title:"Accept edits",      symbol:"\u23F5\u23F5", color:"autoAccept"},
#     bypassPermissions: {title:"Bypass Permissions",symbol:"\u23F5\u23F5", color:"error"},
#     dontAsk:           {title:"Don't Ask",         symbol:"\u23F5\u23F5", color:"error"},
#     auto:              {title:"Auto",             symbol:"\u23F5\u23F5", color:"warning"},
#   }
#
# with `Pin = "\u23F8"` defined a few hundred bytes away. The colour NAMES
# resolve through four theme tables in the same bundle -- light, light
# colour-blind, dark, dark colour-blind. DOXA is a dark theme, so the dark
# table is the one that applies; it is identifiable without guessing
# because it is the one whose `text` is white and whose `claude` is
# rgb(215,119,87), the Claude orange DOXA's own accent already tracks.
#
# Contrast was checked rather than assumed: against the status bar's own
# #221F1A (which stays opaque in BOTH background modes -- the bar does not
# read $doxa-base, see theme.tcss) every value below lands between 4.70
# and 10.07 against WCAG AA's 4.5, all of them better than DOXA's own
# CTX_RED at 4.14.

# U+23F8 PAUSE / U+23F5 BLACK MEDIUM RIGHT-POINTING TRIANGLE, exactly as
# the table above assigns them. The division is the real one: `⏸` for the
# two modes that pause and ask, `⏵⏵` for the four that run something
# without stopping.
MODE_GLYPH = {
    "default": "⏸",
    "plan": "⏸",
    "acceptEdits": "⏵⏵",
    "auto": "⏵⏵",
    "bypassPermissions": "⏵⏵",
    "dontAsk": "⏵⏵",
}

# The dark table's values for the colour NAME each mode maps to, resolved
# once here so the mapping mode -> colour is one hop rather than two:
#   default -> inactive   rgb(153,153,153)
#   plan    -> planMode   rgb(72,150,140)
#   acceptEdits -> autoAccept rgb(175,135,255)
#   auto    -> warning    rgb(255,193,7)
#   bypassPermissions, dontAsk -> error rgb(255,107,128)
MODE_COLOR = {
    "default": "#999999",
    "plan": "#48968C",
    "acceptEdits": "#AF87FF",
    "auto": "#FFC107",
    "bypassPermissions": "#FF6B80",
    "dontAsk": "#FF6B80",
}

# The one place DOXA deliberately diverges from what it measured, and the
# reason is a difference in the PRODUCT, not in taste: Claude Code's own
# Shift+Tab cycler has four entries (default, acceptEdits, plan, auto --
# `T1i` in the same bundle) and cannot reach bypassPermissions at all. In
# DOXA, since v0.50.0, it can: the user asked for it explicitly. A colour
# that is merely "error red" is calibrated for a mode you had to go out of
# your way to select; it is not calibrated for one a mistyped keystroke
# lands on. So the two modes where nothing is checked at all get the same
# hue and one extra step of weight, which costs no columns and survives
# every width. Everything else -- hue, glyph, ordering -- is the measured
# value untouched.
MODE_BOLD = ("bypassPermissions", "dontAsk")

# Full name -> what the chip prints when the terminal is narrow. The
# status bar is the most contended row in the app, and a mode that has
# stopped asking has to stay on it at EVERY width, so it needs a small
# form rather than only a hide rule. Short enough to fit, long enough to
# still read as the mode; the tooltip carries the exact SDK spelling in
# every tier, because that is the string `/mode <name>` takes.
#
# ``default`` is deliberately absent: it is the one mode that never has to
# fit, because a cramped row drops that chip entirely rather than shrinking
# it (_mode_chip_cramped). Giving it an abbreviation here would ship a
# label no user can ever see, which is the same "present, documented,
# dead" failure the Ctrl+Tab measurement talked DOXA out of. The ``.get``
# below falls back to the full name, so nothing breaks if that rule ever
# changes -- it just prints `mode:default`.
MODE_SHORT = {
    "acceptEdits": "edits",
    "plan": "plan",
    "bypassPermissions": "bypass",
    "dontAsk": "no-ask",
    "auto": "auto",
}

# Below this width the chip prints MODE_SHORT instead of the SDK's own
# spelling -- `⏵⏵ mode:bypassPermissions` costs 25 columns and
# `⏵⏵ mode:bypass` costs 14. Same reasoning and the same measured baseline
# as CTX_ABSOLUTE_MIN_COLS above (the ordinary chip set already fills ~95
# columns, and an 80-column bar was measured pushing the reattach handle
# off the row once one more chip joined it), with one difference that
# matters: a mode which has stopped asking SHRINKS here and never
# disappears, because it is the only place that fact is shown. It is the
# safe default that stands down instead -- see
# PaneChipsMixin._mode_chip_cramped, which owns both halves of the rule.
MODE_CHIP_MIN_COLS = 110

# One sentence per mode, in the user's terms rather than the SDK's: the
# question a person actually has in front of a status chip is "does
# anything still ask me before it runs?". Shared by the chip's tooltip,
# the picker's rows and ``/mode``'s own listing, so the three cannot say
# different things about the same mode.
#
# Corroborated against the same bundle: its `Kmr()` maps auto -> "classify",
# bypassPermissions -> "allow", dontAsk -> "deny", everything else -> "ask".
MODE_EXPLAIN = {
    "default": "the CLI asks you before anything it considers dangerous",
    "acceptEdits": "file edits run unasked; everything else still asks",
    "plan": "no tool runs at all — planning only",
    "bypassPermissions": "EVERY tool call runs unapproved; nothing asks you",
    "auto": "a model classifier approves or denies each call instead of you",
    "dontAsk": "anything not pre-approved is DENIED, with no prompt shown",
}


def mode_text(mode: "str | None", *, short: bool = False) -> str:
    """The permission-mode chip's PLAIN text, glyph included.

    Split from :func:`mode_chip` for the reason :func:`ctx_text` is split
    from :func:`ctx_chip`, which is a defect this codebase has already
    paid for once: ``StatusBar._tooltip_for_x`` resolves a chip's tooltip
    by finding the chip's text inside the bar's markup-STRIPPED string, so
    a key that still carries ``[#FF6B80]…[/]`` matches nothing and the
    tooltip silently vanishes at exactly the tier where it matters most
    (v0.35.0, the ctx chip's amber and red tiers). The GLYPH belongs on
    this side of that split, not the colour side: it is text, it survives
    markup stripping, and the tooltip has to key on the same string the
    widget paints.

    An unrecognised mode is printed verbatim and glyphless rather than
    mapped to "default": if the CLI grows a seventh mode, a chip that lies
    about which one is in force is worse than one showing a name DOXA does
    not know."""
    from .. import engine as engine_mod

    name = str(mode or engine_mod.DEFAULT_PERMISSION_MODE)
    label = MODE_SHORT.get(name, name) if short else name
    glyph = MODE_GLYPH.get(name, "")
    return f"{glyph} mode:{label}" if glyph else f"mode:{label}"


def mode_chip(mode: "str | None", *, short: bool = False) -> str:
    """The permission-mode chip's MARKUP -- Claude Code's colour for this
    mode, bold for the two where nothing is checked at all.

    Every mode is coloured, including ``default``: that is what the
    measured table does (``inactive``, a grey), and it is also what makes
    the chip readable as one object rather than as a word that sometimes
    lights up. Unlike :func:`ctx_chip`, this returns no uncoloured tier at
    all, so the caller's clickable accent never shows through -- the mode
    signal owns this chip's colour outright, which is the point of
    matching another client's palette in the first place."""
    from .. import engine as engine_mod

    name = str(mode or engine_mod.DEFAULT_PERMISSION_MODE)
    text = mode_text(name, short=short)
    colour = MODE_COLOR.get(name)
    if colour is None:
        return text
    weight = "bold " if name in MODE_BOLD else ""
    return f"[{weight}{colour}]{text}[/]"


def mode_tooltip(mode: "str | None") -> str:
    """The chip's hover row: what this mode DOES, in the terms the user
    cares about (does anything still ask me?), plus the exact SDK spelling
    so ``/mode <name>`` is copyable from the tooltip, plus the key.

    Unconditional and full in every width tier -- the same discipline
    ``_ctx_tooltip_absolute`` follows. What the chip gives up to fit is
    never what the tooltip gives up. The cycle is spelled out from the
    constant rather than written into this string, so a mode joining or
    leaving the hotkey cannot leave a stale list here."""
    from .. import engine as engine_mod

    name = str(mode or engine_mod.DEFAULT_PERMISSION_MODE)
    what = MODE_EXPLAIN.get(name, "a permission mode DOXA does not know")
    cycle = " → ".join(engine_mod.CYCLE_MODES)
    tail = f"click to change, or /mode <name>; Shift+Tab cycles {cycle}"
    return f"permission mode {name} — {what} · {tail}"


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


#: How wide one CHIP-PICKER row runs. Unchanged since v0.27.0, and the
#: number is load-bearing rather than aesthetic: the picker is an
#: OptionList with a border and a one-column margin, and the ` ▸ ` selection
#: mark costs three more, so 72 is what fits an 80-column terminal without
#: the row overflowing its own dropdown.
#:
#: Item V added a stamp segment in front of the claim and did NOT widen
#: this to make room. The picker is the glance; the browser
#: (doxa.ui.beliefs, CLAIM_WIDTH) is the full-width surface, and both of
#: them are backstopped by a tooltip carrying the claim whole.
PICKER_ROW_WIDTH = 72


def _fmt_belief_row(belief: dict) -> str:
    """One beliefs-picker row: when it was created, how long since anything
    touched it, then the claim text, ellipsized -- filtering (ChipPicker's
    type-to-filter) matches against exactly this string, so the claim has
    to be IN it, not just referenced by an id.

    The stamp segment is emitted only when the belief actually CARRIES
    timestamps (item V added them to ``list_beliefs``' SELECT). A row that
    has none renders exactly as it did before rather than growing a
    placeholder column that says nothing -- and that is also what keeps a
    belief arriving from an older daemon, over the wire, honest."""
    stamp = belief_stamp(belief)
    claim = _one_line(str(belief.get("claim") or ""), 200)
    return ellipsize(f"{stamp} · {claim}" if stamp else claim, PICKER_ROW_WIDTH)


def _fmt_pending_row(item: "dict | str") -> str:
    """One ``/pending`` row: WHAT APPROVING IT WOULD DO, how long it has
    been waiting, then the proposal's own text, ellipsized.

    Item V's requirement in one line -- "a row that does not say what
    approving it changes is not reviewable". The verdict comes first
    because it is the part a reviewer scans; the text is still in the
    string because ChipPicker's type-to-filter matches this exact label
    and a row you cannot search by its own words is not a row.

    Accepts a bare string as well as a record: see :func:`as_proposal`."""
    proposal = as_proposal(item)
    verdict = proposal_verdict(proposal)
    age = proposal_age_text(proposal)
    text = _one_line(proposal_text(proposal), 200)
    lead = " · ".join(part for part in (verdict, age) if part)
    return ellipsize(f"{lead} · {text}" if lead else text, PICKER_ROW_WIDTH)


# -- item V: timestamps, age, provenance, and the proposed verdict --------
#
# The beliefs browser needs four things the v0.27.0 picker never carried,
# and all four are pure functions of one record, so they live here beside
# the row formatters that already read those records rather than inside
# the widget that paints them.
#
# WHICH TIMESTAMP, and WHAT STALENESS ACTUALLY IS (corrected in v0.46.0).
#
# The belief store keeps three timestamps on the `beliefs` row itself
# (lore_core.store: created, updated, last_referenced) and v0.40.0 built
# the staleness column out of `coalesce(last_referenced, updated)` --
# LORE's own dormancy-sweep expression. That was the wrong clock, and the
# user said so: "staleness is rather indicated by whether or not the
# belief was confirmed ... recently or not".
#
# The distinction is real. `last_referenced` moves when a belief is merely
# INJECTED or CITED -- the agent reading a claim back to itself is not
# evidence the claim is still true. What makes a belief still-true is that
# reality tested it, and LORE keeps that in a different table:
# `belief_outcomes`, one append-only row per verdict, `event` constrained
# by the schema to 'confirmed' / 'contradicted' / 'stale'. That ledger is
# the ground truth `calibrated_confidence` calibrates the deriver's
# self-report against; it is the honest staleness signal and this module
# now paints it.
#
# So a belief row carries:
#
#   created          as an absolute date -- "how old is this belief" read
#                    literally, and the only fact here that never moves.
#   last outcome     its EVENT and the age of that event: "confirmed 2d",
#                    "contradicted 2d", "stale 40d". The verdict is shown,
#                    never just the age -- confirmed and contradicted are
#                    opposite facts about the same belief and must not
#                    render alike.
#   never tested     its own state, NOT a large age. MEASURED on this
#                    machine: 31 outcome rows against 628 active beliefs,
#                    so ~95% of a real store has never been tested at all.
#                    Rendering one of those as "120d idle" asserts
#                    something false -- nothing went stale, nothing was
#                    ever checked.
#
# `last_referenced` is NOT on the row any more. It is not worthless -- a
# belief cited three days ago and never once confirmed is a real and
# interesting state -- but it is the third-most-important thing on the
# line, and two age-shaped numbers side by side, only one of which means
# anything, is exactly the confusion this correction removes. It moved to
# the tooltip, where per-belief detail already lives.

#: ``lore_core.config.utcnow``'s format -- the one every timestamp in the
#: belief store and every ``created`` in a staged proposal is written in.
LORE_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_lore_time(stamp: "str | None") -> "float | None":
    """A lore timestamp as a UNIX epoch, or None when it is absent or in
    some shape this function did not measure. Never raises and never
    guesses: an unparseable stamp produces no age row rather than an age
    computed from a date nobody wrote."""
    text = str(stamp or "").strip()
    if not text:
        return None
    from datetime import datetime, timezone

    try:
        parsed = datetime.strptime(text, LORE_TIME_FORMAT)
    except ValueError:
        try:  # a store written by some other ISO-8601 producer
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _age_of(stamp: "str | None", now: "float | None" = None) -> "float | None":
    """Seconds since ``stamp``, or None when there is no usable stamp.
    Clamped at zero -- a clock skew must not paint a belief as being from
    the future, which reads as a bug in the belief rather than in the
    clock."""
    when = _parse_lore_time(stamp)
    if when is None:
        return None
    import time as _time

    return max(0.0, (now if now is not None else _time.time()) - when)


def belief_touched(belief: dict) -> "str | None":
    """The belief's staleness clock: ``last_referenced``, falling back to
    ``updated`` -- lore_core's own coalesce, see this section's lead."""
    return (
        str(belief.get("last_referenced") or "").strip()
        or str(belief.get("updated") or "").strip()
        or None
    )


def belief_created_text(
    belief: dict, *, full: bool = False, now: "float | None" = None,
) -> str:
    """When the belief entered the store, to the MINUTE.

    v0.46.0 showed the date alone and argued that "a belief store is
    browsed by day, and the seconds are noise in a column that has to fit
    beside a claim". The user overruled the first half and kept the second:
    HH:MM, no seconds. Seconds on a claim derived by a background reviewer
    are a precision nobody acts on.

    WHAT GAVE WAY FOR THE SIX COLUMNS. ``PICKER_ROW_WIDTH`` is 72 because
    that is what fits an 80-column terminal inside a bordered dropdown, so
    a wider stamp is columns taken from the claim -- and the claim is what
    ChipPicker's type-to-filter matches against, so narrowing it narrows
    what is findable. Rather than spend six, the picker drops the YEAR from
    a belief derived in the current one: `08-25 14:23` costs ONE column
    more than `2026-08-25` did, and a year the reader is standing in is the
    least informative thing on the line. A belief from an earlier year
    keeps its year and costs the full six, which is exactly the case where
    the year is worth having. Same convention `ls -l` has used for decades.

    ``full=True`` disables the elision -- the browser's own rows have the
    width and are read as a record rather than as a glance, so they always
    carry `YYYY-MM-DD HH:MM`. Both surfaces show HH:MM; only the picker
    infers the year."""
    stamp = str(belief.get("created") or "").strip()
    when = _parse_lore_time(stamp)
    if when is None:
        # Unparseable but present: show whatever date-shaped prefix it has
        # rather than dropping the column, and never invent a clock time
        # for a string this function could not read.
        return stamp[:10]
    import time as _time

    parts = _time.gmtime(when)
    if full:
        return _time.strftime("%Y-%m-%d %H:%M", parts)
    this_year = _time.gmtime(now if now is not None else _time.time()).tm_year
    return _time.strftime(
        "%m-%d %H:%M" if parts.tm_year == this_year else "%Y-%m-%d %H:%M", parts
    )


def belief_age_text(belief: dict, now: "float | None" = None) -> str:
    """"34d idle" -- how long since anything REFERENCED or updated this
    belief. Empty when neither timestamp is present.

    No longer the row's staleness column (v0.46.0 -- see this section's
    lead: being cited is not being confirmed). It survives because the
    tooltip still reports it, and because "cited 3d ago, never once
    confirmed" is a state worth being able to read."""
    secs = _age_of(belief_touched(belief), now)
    return f"{_fmt_age(secs)} idle" if secs is not None else ""


#: LORE's own outcome vocabulary, verbatim: ``belief_outcomes.event`` is
#: CHECK-constrained to exactly these three in ``lore_core.store``. Not a
#: DOXA spelling of them -- a verdict this app invented would be a verdict
#: nothing in the store can ever produce.
OUTCOME_EVENTS: "tuple[str, ...]" = ("confirmed", "contradicted", "stale")

#: What a belief with an outcome ledger but no rows in it says. A WORD,
#: never a duration: it is a distinct state, not a large age, and the
#: whole point of the v0.46.0 correction is that those two must not be
#: mistakable for each other.
NEVER_TESTED = "never tested"

#: Per-verdict colour. "confirmed 2d" and "contradicted 2d" differ by one
#: word at a glance and by their whole meaning in fact, so they differ by
#: colour too; never-tested wears the muted body colour because it is an
#: ABSENCE of signal rather than a bad one, and must not read as an alarm.
OUTCOME_COLORS: "dict[str, str]" = {
    "confirmed": "#7A9B6E",     # the shell block's green -- worn by nothing else
    "contradicted": CTX_RED,
    "stale": CTX_AMBER,
    "untested": "#8A8073",      # .system-block's muted text
}


def belief_outcome_kind(belief: dict) -> str:
    """Which of the four states this belief is in, or ``""`` when the
    record cannot say.

    ``"confirmed"`` / ``"contradicted"`` / ``"stale"`` -- LORE's own
    verdicts, from the most recent row of the belief's outcome ledger.
    ``"untested"`` -- the ledger was read and is empty for this belief.
    ``""`` -- the record carries no ``outcomes`` field at all, which means
    it came from something that predates this column (an older daemon
    across the socket). That renders as NO column rather than as a guessed
    one, the same rule ``belief_provenance`` follows for a NULL ``via``."""
    if belief.get("outcomes") is None:
        return ""
    event = str(belief.get("outcome_event") or "").strip().lower()
    if event in OUTCOME_EVENTS:
        return event
    return "untested"


def belief_outcome_text(belief: dict, now: "float | None" = None) -> str:
    """The staleness column: LORE's verdict and how long ago it landed --
    ``"confirmed 2d"``, ``"contradicted 2d"``, ``"stale 40d"`` -- or
    :data:`NEVER_TESTED`. Empty for a record with no ledger field."""
    kind = belief_outcome_kind(belief)
    if not kind:
        return ""
    if kind == "untested":
        return NEVER_TESTED
    secs = _age_of(belief.get("outcome_at"), now)
    return f"{kind} {_fmt_age(secs)}" if secs is not None else kind


def belief_outcome_color(belief: dict) -> str:
    return OUTCOME_COLORS.get(belief_outcome_kind(belief), "")


def belief_outcome_tally(belief: dict) -> str:
    """"2 confirmed, 1 contradicted" -- the whole ledger for one belief, in
    LORE's own vocabulary and LORE's own counts (see
    ``lore_core.beliefs.outcome_counts``, which
    ``SessionEngine.list_beliefs`` is pinned equal to by a test). Empty
    when there is nothing to tally."""
    parts = [f"{belief[f'outcome_{event}s']} {event}"
             for event in OUTCOME_EVENTS
             if isinstance(belief.get(f"outcome_{event}s"), int)
             and belief[f"outcome_{event}s"] > 0]
    return ", ".join(parts)


def belief_sort_key(belief: dict) -> "tuple[int, float]":
    """Inside one scope group: beliefs REALITY HAS TESTED first, most
    recently tested first; everything else after them, untouched.

    Not a cosmetic ordering. 31 outcome rows against 628 active beliefs
    means the tested ones are needles, and a browser that interleaves them
    with six hundred never-tested claims by date has hidden the only
    evidence it holds. Never-tested sorts as a BUCKET rather than by age,
    because it is a state and not a large age -- and Python's sort is
    stable, so inside that bucket ``list_beliefs``' own ``updated DESC``
    order survives untouched."""
    secs = _age_of(belief.get("outcome_at"))
    if belief_outcome_kind(belief) in OUTCOME_EVENTS and secs is not None:
        return (0, secs)
    return (1, 0.0)


def belief_stamp(
    belief: dict, now: "float | None" = None, *, full: bool = False,
) -> str:
    """The row's two facts as one segment: when the belief was created, and
    what reality last said about it."""
    return " · ".join(
        part for part in (belief_created_text(belief, full=full, now=now),
                          belief_outcome_text(belief, now))
        if part
    )


def belief_provenance(belief: dict) -> str:
    """How this belief got into the store, in LORE's own vocabulary.

    ``via`` is lore_core 0.36.0's provenance column (ISSUE #43): "derived"
    (the deriver), "dream" (the reconciler), "direct" (a trusted CLI
    write), "approved" (a staged proposal a human applied). It is NULL on
    every belief written before that release and is deliberately never
    back-filled there, so DOXA does not back-fill it either -- an
    unlabelled belief reads "provenance unknown", which is a statement
    about the ledger, not about the belief."""
    via = str(belief.get("via") or "").strip()
    return f"via {via}" if via else "provenance unknown"


def belief_tooltip(belief: dict) -> str:
    """What hovering a belief row shows: the claim IN FULL, then the facts
    the row had to abbreviate.

    The user asked for the full belief text on hover, and that is what
    leads here -- a row is a glance, and the whole claim without losing
    your place is the point. The evidence TRAIL is deliberately not in
    here: it is an unbounded list of session ids and notes, it is fetched
    lazily per belief (see ``SessionEngine.belief_evidence``), and a
    tooltip that has to wait on a query is a tooltip that flickers. The
    trail lives one keystroke away, in the row's own expansion."""
    lines = [_one_line(str(belief.get("claim") or ""), 4000) or "(no claim text)"]
    if belief.get("claim_truncated"):
        lines.append("[claim truncated — larger than one wire frame]")
    subject = str(belief.get("subject") or "")
    confidence = belief.get("confidence")
    conf = f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "?"
    meta = [f"{_belief_scope_label(subject)} ({subject})" if subject else "",
            f"confidence {conf}", belief_provenance(belief)]
    lines.append("")
    lines.append(" · ".join(part for part in meta if part))
    created = belief_created_text(belief)
    if created:
        lines.append(f"created {created}")

    # The staleness answer, said in full and said plainly. A never-tested
    # belief gets a SENTENCE rather than a token here, because the row had
    # only two words to spend on it and "never tested" is the fact a
    # reader is most likely to want spelled out.
    kind = belief_outcome_kind(belief)
    if kind == "untested":
        lines.append(
            "never tested — no outcome has ever been recorded for this "
            "belief, so nothing has confirmed it and nothing has "
            "contradicted it"
        )
    elif kind:
        at = str(belief.get("outcome_at") or "").strip()
        secs = _age_of(at)
        ago = f" ({_fmt_age(secs)} ago)" if secs is not None else ""
        source = str(belief.get("outcome_source") or "").strip()
        by = f", recorded by {source}" if source else ""
        lines.append(f"last outcome: {kind} {at}{ago}{by}")
        tally = belief_outcome_tally(belief)
        if tally:
            lines.append(f"outcome ledger: {tally}")

    # Being REFERENCED is not being confirmed -- that is the whole v0.46.0
    # correction -- so this sits below the outcome and never beside it.
    touched = belief_touched(belief)
    if touched:
        secs = _age_of(touched)
        age = f" ({_fmt_age(secs)} ago)" if secs is not None else ""
        lines.append(f"last referenced {touched}{age} — cited, not confirmed")
    return "\n".join(lines)


def as_proposal(item: "dict | str") -> dict:
    """A staged proposal as a RECORD, whatever shape it arrived in.

    ``/pending`` carried bare strings from v0.31.0 until item V, and a
    detached session talks to a daemon that may still be the older build
    (the socket outlives an upgrade -- a running daemon is not restarted
    by installing a new DOXA). A proposal that arrives as text still
    renders as a row; it simply has no verdict to show, which the row then
    says by omission rather than by inventing one."""
    if isinstance(item, dict):
        return item
    return {"text": str(item or "")}


#: What a proposal's ``kind``/``action`` pair DOES, as a verb a reviewer
#: can act on. Read off lore_core.pending.apply_item -- the function that
#: actually performs each of these -- rather than invented here, so a
#: verdict row and the write it predicts cannot disagree. ``add`` is the
#: default for a proposal with no action at all: that is apply_item's own
#: fallback ("`add` stays its meaning -- so every proposal written before
#: 0.36.0 applies exactly as it always did").
PROPOSAL_ACTIONS: "dict[str, str]" = {
    "add": "add",
    "replace": "replace",
    "remove": "remove",
    "move": "move",
    "retract": "retract",
    "update": "update",
    "retire": "retire",
}


def proposal_action(item: dict) -> str:
    action = str(item.get("action") or "").strip().lower()
    return PROPOSAL_ACTIONS.get(action, action or "add")


def proposal_target(item: dict) -> str:
    """WHERE the write would land -- the store, and the scope inside it."""
    kind = str(item.get("kind") or "").strip().lower()
    if kind == "memory":
        scope = str(item.get("scope") or "").strip()
        slug = str(item.get("project") or "").strip()
        if scope == "project" and slug:
            return f"memory/project:{slug}"
        return f"memory/{scope}" if scope else "memory"
    if kind == "belief":
        subject = str(item.get("subject") or "").strip()
        return f"belief/{subject}" if subject else "belief"
    if kind == "filemap":
        slug = str(item.get("project") or "").strip()
        return f"filemap/{slug}" if slug else "filemap"
    if kind == "skill" or item.get("name"):
        name = str(item.get("name") or "").strip()
        return f"skill/{name}" if name else "skill"
    return kind or "?"


def proposal_verdict(item: "dict | str") -> str:
    """The PROPOSED VERDICT: what approving this row would actually do,
    as one short phrase -- ``add → memory/user``, ``retract → belief #42``.

    Empty for a proposal that arrived as bare text with no record behind
    it (:func:`as_proposal`): no verdict is the honest answer there, and a
    guessed one on a write path is exactly the wrong place to guess."""
    proposal = as_proposal(item)
    kind = str(proposal.get("kind") or "").strip()
    if not kind and not proposal.get("action"):
        return ""
    action = proposal_action(proposal)
    if kind == "belief" and action == "retract":
        return f"retract → belief #{proposal.get('id')}"
    return f"{action} → {proposal_target(proposal)}"


def proposal_supersedes(item: dict) -> str:
    """The existing entry this proposal would displace, when it names one
    -- ``replace``/``remove``/``move`` all carry a ``match`` needle, and
    ``retract`` names a belief id. Empty when nothing is superseded, which
    is what an ``add`` is."""
    action = proposal_action(item)
    if action == "retract":
        bid = item.get("id")
        return f"belief #{bid}" if bid is not None else ""
    needle = str(item.get("match") or "").strip()
    if not needle:
        return ""
    return _one_line(needle, 200)


def proposal_text(item: dict) -> str:
    """The proposal's own body, whichever field its kind keeps it in --
    the same precedence ``lore_core.pending.cmd_pending`` prints."""
    for key in ("text", "claim", "purpose", "description"):
        value = str(item.get(key) or "").strip()
        if value:
            if key == "purpose" and item.get("path"):
                return f"{item['path']} — {value}"
            return value
    for key in ("path", "name"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def proposal_age_text(item: dict, now: "float | None" = None) -> str:
    """"staged 5d ago". Empty when the proposal carries no ``created``
    stamp -- everything the deriver and the write gate stage does."""
    secs = _age_of(item.get("created"), now)
    return f"staged {_fmt_age(secs)} ago" if secs is not None else ""


def proposal_tooltip(item: "dict | str") -> str:
    """Hovering a proposal row: its full text, then exactly what approving
    it would change -- the same "the whole thing without losing your
    place" rule :func:`belief_tooltip` follows."""
    proposal = as_proposal(item)
    lines = [_one_line(proposal_text(proposal), 4000) or "(no proposal text)"]
    if proposal.get("text_truncated"):
        lines.append("[proposal truncated — larger than one wire frame]")
    lines.append("")
    verdict = proposal_verdict(proposal)
    lines.append(
        f"approving this would: {verdict}" if verdict
        else "this proposal arrived without a record, so what approving it "
             "would do cannot be shown"
    )
    supersedes = proposal_supersedes(proposal)
    if supersedes:
        lines.append(f"superseding: {supersedes}")
    if proposal.get("to"):
        lines.append(f"moving it to: project:{proposal['to']}")
    age = proposal_age_text(proposal)
    created = str(proposal.get("created") or "").strip()
    if age:
        lines.append(f"{age} ({created})" if created else age)
    session = str(proposal.get("session_id") or "").strip()
    if session:
        lines.append(f"from session {session}")
    writer = str(proposal.get("writer") or "").strip()
    if writer:
        lines.append(
            f"!! staged by LORE's write gate: {writer} context "
            f"({proposal.get('writer_evidence') or 'no evidence recorded'})"
        )
    note = str(proposal.get("cross_project_note") or "").strip()
    if note:
        lines.append(f"!! {note}")
    return "\n".join(lines)


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
    """One age, one format, everywhere -- session uptime, cache staleness,
    (item V) how long a belief has sat untouched or a proposal unreviewed,
    and (v0.45.0) every row of the ``/search`` popup.

    The DAY tier is item V's addition and the reason there is still only
    one of these functions. Beliefs and staged proposals are months old,
    not hours: rendering a four-month-old belief as "2904h0m" is a number
    a reader has to do arithmetic on, which is the opposite of what an age
    column is for. Session history has the same shape and hit the same
    wall -- last Tuesday came out "168h0m" -- so /search reuses this
    rather than spelling "how long ago" a second way.

    Everything under a day is unchanged, so every existing caller renders
    exactly as it did. What the tier's own ``days < 10`` cut-off buys is a
    ceiling: nothing this returns exceeds five columns, which is what lets
    /search park an age in a fixed gutter beside an excerpt without ever
    costing that excerpt a column."""
    if secs < 60:
        return f"{int(max(0.0, secs))}s"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h{int((secs % 3600) // 60)}m"
    days = int(secs // 86400)
    hours = int((secs % 86400) // 3600)
    return f"{days}d{hours}h" if days < 10 else f"{days}d"


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


def memory_entries(scope: str, project: "str | None" = None) -> "int | None":
    """How many curated-memory ENTRIES a scope holds, or None if unknown.

    The companion figure to :func:`memory_fill` above, and read from the
    SAME file by lore_core's own ``read_entries`` -- so "9 entries, 39%
    full" is two views of one file rather than two numbers that can
    disagree. Counting `- ` lines here instead would be doxa reimplementing
    lore_core's storage format, which is exactly how the two drift.

    Deliberately UNCACHED, where memory_fill is cached on mtime. That
    cache exists because ``_refresh_status`` runs several times a second
    and must not add file reads; this function has one caller, the opening
    identity block, drawn once per session boot (and again only after an
    auth flow re-renders it). One read of an ≤8800-character file, once,
    does not need a cache -- and a second cache keyed the same way is a
    second thing to invalidate.

    Same failure policy as memory_fill: absent means omitted, never
    invented."""
    try:
        from lore_core import memory as lore_memory

        path = lore_memory.memory_path(scope, project or "")
        if not path.exists():
            return None
        return len(lore_memory.read_entries(path))
    except Exception:
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

# SPDX-License-Identifier: AGPL-3.0-only
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


# -- the diff's own vocabulary (v1.0.1) -------------------------------
#
# Added is green, removed is red, and it says so in the SAME two colours
# on both surfaces that report a diff: the pane's hunk bodies and file
# folds (doxa.ui.diffview) and the status chip's `+42 −7`
# (PaneChipsMixin._status_chips). One vocabulary, defined once, because
# the chip is a summary OF the pane and two palettes would make them
# look like two different features.
#
# ONE THEME, and that is a fact about this app rather than a shortcut:
# DOXA registers no Theme at all (DoxaApp.get_theme_variable_defaults'
# own docstring) and theme.tcss is a single warm DARK ramp -- the only
# variation is $doxa-base, opaque #171512 or `ansi_default` for a
# terminal the user wants to show through. So there is no light/dark
# pair to define here; these are picked to read against that dark ramp,
# exactly as every other colour in this module is, and the `background`
# setting's own note already records that a LIGHT terminal background
# renders DOXA's body text at very low contrast regardless.
#
# The two FG/BG pairs are the ones that had to be measured rather than
# chosen: v0.92.0 coloured a changed line's foreground only (#7FB069 /
# #D08770 on the ramp), and a foreground on the ramp is not a background
# -- putting those same two hues BEHIND the text would have left the
# text unreadable on itself. So the backgrounds are deep, desaturated
# washes two steps off the ramp, and each carries a foreground chosen
# for contrast against IT rather than inherited from the row above.
DIFF_ADD_NUM = "#7FB069"     # the added-line number, and `+42` on the chip
DIFF_DEL_NUM = "#D08770"     # the removed-line number, and `−7` on the chip
DIFF_ADD_FG = "#DCEBD3"      # added body text, on the wash below
DIFF_ADD_BG = "#1E3222"
DIFF_DEL_FG = "#F3D6CF"      # removed body text, on the wash below
DIFF_DEL_BG = "#3B211E"
DIFF_CONTEXT_FG = "#B4AB9E"  # unchanged body: the ramp's own secondary
DIFF_GUTTER_FG = "#6E6459"   # a context row's line numbers: present, quiet
DIFF_RULE_FG = "#4A443C"     # the side-by-side separator
DIFF_NOTE_FG = "#8A8073"     # the `\ No newline` note, the truncation note


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
def _strip_holding(app: Any, tab_id: str) -> "Any":
    """The tab strip that holds this tab, of the N a window now has.

    v0.97.0: through v0.95.0 there was exactly one strip and
    ``query_one("#session-tabs")`` was the right way to name it. Every pane
    GROUP owns one now, so "the strip" is always a question about which
    group -- and a status write aimed at a background group's tab must land
    there and not on the foreground one's."""
    finder = getattr(app, "tabbed_holding", None)
    if callable(finder):
        return finder(tab_id)
    # A harness that mounted these widgets without a DoxaApp around them.
    # One strip, the old shape, and the old answer.
    with contextlib.suppress(Exception):
        return app.query_one(TabbedContent)
    return None


def _write_tab_label(app: Any, tab_id: str, text: str) -> None:
    """Write `text` straight onto one Tab's label -- no provider glyph.
    SessionPane.set_tab_label prepends one deliberately (every SESSION is
    Claude/Anthropic's); a subagent transcript tab is not a session, so it
    never goes through that path at all -- this is its own, glyph-free,
    door onto the same tab strip."""
    with contextlib.suppress(Exception):
        _strip_holding(app, tab_id).get_tab(tab_id).label = text


def _write_tab_class(app: Any, tab_id: str, class_name: str, value: bool) -> None:
    """Toggle one status class on one group's Tab header. Shared by
    SessionPane (-working/-done-unseen/-attention) and
    SubagentTranscriptTab (-done-unseen only) -- same contextlib.suppress
    discipline either caller needs: the tab may not exist yet this early,
    or may already be mid-teardown."""
    with contextlib.suppress(Exception):
        _strip_holding(app, tab_id).get_tab(tab_id).set_class(value, class_name)


#: Every "you missed something" class a session can carry, in the ORDER
#: doxa/theme.tcss resolves them in (done-unseen < staged < working <
#: attention -- equal selector specificity, later rule wins).
#:
#: v1.0.0: the session sidebar shows the same four marks per row, and
#: shows them by writing THESE class names onto the row and letting the
#: SAME cascade decide, rather than by picking a winner in Python. One
#: statement of what a mark outranks, in the stylesheet, read by two
#: surfaces -- which is the "one source, read twice" rule
#: docs/plans/session-sidebar.md names as the risk of a second place that
#: renders session state.
TAB_STATE_MARKS: "tuple[str, ...]" = (
    "-done-unseen", "-staged", "-working", "-attention",
)


#: One glyph per mark, for the sidebar row that carries it. The tab strip
#: signals with COLOUR alone, which it can afford: a strip has two columns
#: of padding per header and no room for more. A rail row has a column to
#: spend, and spending it means the rail still says something on a
#: monochrome terminal and to a reader who cannot separate the green of
#: done-unseen from the amber of working.
SIDEBAR_MARK_GLYPHS: "dict[str, str]" = {
    "-done-unseen": "✓",
    "-staged": "+",
    "-working": "▸",
    "-attention": "!",
}

#: What a row with no mark at all shows, so every label starts in the
#: same column whether or not its session has news.
SIDEBAR_MARK_NONE = " "


def top_mark(marks: Any) -> str:
    """The ONE mark that wins, out of however many a session carries.

    Precedence is :data:`TAB_STATE_MARKS`' own order and is not restated
    here -- that tuple is the single written-down statement of what
    outranks what, and doxa/theme.tcss resolves the row's COLOUR by
    cascading the same four classes in the same sequence. So the glyph
    and the colour cannot disagree about which state a row is in without
    one of them disagreeing with the tab strip too."""
    winner = ""
    held = set(marks or ())
    for name in TAB_STATE_MARKS:
        if name in held:
            winner = name
    return winner


def sidebar_mark_glyph(marks: Any) -> str:
    """The leading glyph for a sidebar row -- a space when there is
    nothing to say, so labels stay in one column."""
    return SIDEBAR_MARK_GLYPHS.get(top_mark(marks), SIDEBAR_MARK_NONE)


def mark_over(leaves: Any, class_name: str) -> bool:
    """Does ANY of these session leaves carry that mark?

    The one derivation of a tab header's status, extracted in v1.0.0 so
    the sidebar can call it instead of writing a second one.
    :meth:`doxa.session.pane.SessionPane._set_tab_class` has ORed a
    tab's leaves since v0.91.0 (a tab could hold several panes then; it
    holds one now, and the OR is still the correct general statement);
    a sidebar row ORs the single pane behind that row, which is the same
    question asked of a shorter list.

    Reads ``has_mark`` where a leaf offers one and falls back to the
    ``_marks`` dict itself, so an ``ArchivedSessionTab`` -- which has
    neither, and no state to report -- answers False rather than
    raising."""
    for leaf in leaves or ():
        asker = getattr(leaf, "has_mark", None)
        if callable(asker):
            if asker(class_name):
                return True
            continue
        if getattr(leaf, "_marks", {}).get(class_name, False):
            return True
    return False


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

# The diff chip (v1.0.1) shortens at the SAME width, and the equality is
# deliberate rather than coincidental: it is one measurement of one row,
# not two chips that each guessed. Below this the chip drops its noun
# (`diff 3 files +42 −7` -> `diff 3f +42 −7`), eight columns back on the
# row for a word a reader in front of `3f +42 −7` does not need. It
# inherits the mode chip's asymmetry too: the two states that mean
# "cannot tell" (`diff ⚠ no base`, `diff ⚠ unreadable`) neither shorten
# nor stand down at any width, because they are the only place that fact
# appears -- see doxa.diff.DiffCounts.chip, which owns the wording, and
# PaneChipsMixin._diff_chip_cramped, which owns this half.
DIFF_CHIP_MIN_COLS = MODE_CHIP_MIN_COLS

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


def mode_tooltip(mode: "str | None", armed: bool = False) -> str:
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
    cycle = " → ".join(engine_mod.cycle_modes(armed))
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


#: The CHANNEL RULE, verbatim from lore_core's own schema comment
#: (``lore_core/deriver.py``: ``"user (the user STATED it) | project |
#: user-model (you INFERRED it from behaviour)"``) and its deriver
#: docstring (``deriver.py:221-230``). Not this app's own wording --
#: LORE refuses to merge the two subjects for exactly this reason, and a
#: user-model belief that READS as authoritative in the UI is the
#: failure that separation exists to prevent. The operative difference
#: is not confidence or age: it is whether a future session may ACT on
#: the claim.
BELIEF_CHANNEL_RULE: "dict[str, str]" = {
    "user": (
        "stated — the user said this themselves, in their own words; a "
        "later session may ACT on it"
    ),
    "user-model": (
        "inferred — derived from how a session went, never spelled out; "
        "shapes tone and approach and authorizes nothing"
    ),
}


def _belief_scope_label(subject: str) -> str:
    """Which GROUP a belief's row falls under in the beliefs chip's picker
    (item 3) -- derived from lore_core's own subject vocabulary
    (``lore_core.beliefs.
    belief_subject``: ``"user"``, ``"user-model"``, or ``"project:<slug>"``
    -- verified against the installed lore_core, there is no separate
    ``scope`` column), data-driven rather than a hardcoded two-way branch
    so a future subject prefix (LORE issue #41's proposed ``machine:<id>``,
    still an open, unimplemented proposal -- NOT built here) slots into
    its own group the moment lore_core starts writing one, with no change
    to this function. ``"user-model"`` stays its own group (interaction-
    model beliefs, never folded into plain "user") -- the same distinction
    belief_subject's own docstring draws.

    The ``· stated``/``· inferred`` suffix (v0.67.0) makes LORE's CHANNEL
    RULE legible wherever this label appears, not only in a tooltip a
    mouse has to find: a user-model claim that reads as authoritative in
    a glance-only surface like the picker is the exact failure the
    channel split exists to prevent. See :data:`BELIEF_CHANNEL_RULE` for
    the rule those two words stand for. ``"project"`` carries no such
    suffix -- it is scoped by repo, not by how the claim was arrived at,
    and has no channel distinction to surface."""
    if subject == "user-model":
        return "user-model · inferred"
    if ":" in subject:
        return subject.split(":", 1)[0]  # "project:<slug>" -> "project"
    return f"{subject or 'user'} · stated"


#: FLOOR for one chip-picker row, and since v0.57.0 only a floor.
#:
#: It WAS the row width from v0.27.0 to v0.48.0: 72 is what fits an
#: 80-column terminal inside a bordered dropdown once the ` ▸ ` mark is
#: paid for. That cut a claim at 72 on a 160-column terminal too, which
#: the user reported. The row is now trimmed by :class:`ChipPicker`
#: against its OWN measured content width -- the widget is the only thing
#: that knows how much room it has, and v0.49.0's banner work already paid
#: for guessing chrome instead of measuring it (a scrollbar moves the
#: budget by two).
#:
#: What survives here is the fallback for the unmeasurable case: a picker
#: rendering before its first layout has no width to read, and 72 is the
#: number that was safe for five releases.
PICKER_ROW_WIDTH = 72

#: How much of a claim or proposal ever reaches a picker row, before the
#: widget trims it to fit. A bound, not a layout decision -- a row label is
#: a string in a list, and one belief with a 40KB claim should not put 40KB
#: through every render pass.
PICKER_ROW_MAX = 400

# -- the ONE row shape both LORE chip pickers render (post-v0.67.0) -------
#
# The beliefs picker and the proposals picker used to format their own
# rows independently -- "stamp · outcome age · claim" for one, "verdict ·
# age · text" for the other -- and drifted: the proposals row had no fixed
# columns at all (a `` · `` join drifts with every field's own length), so
# neighbouring rows never lined up. One formatter now, used by both:
#
#     YY-MM-DD HH:MM   status   age   text
#
# STAMP, STATUS and AGE are fixed-width -- padded, never omitted, so a
# record with nothing to say in one of them (a belief never tested, a
# proposal with no verdict) still holds the column open with blanks rather
# than shifting the text start left. TEXT is the one variable-width field,
# capped to :data:`PICKER_TEXT_CAP` or the ROOM LEFT after the fixed
# prefix (and, for the two menus that carry them, the row's own inline
# actions -- see ``ChipPicker._action_reserve``) at the caller's measured
# width, whichever is smaller -- so a wide terminal always shows up to 100
# columns of claim/proposal text, and a narrow one shrinks the text
# column rather than ever pushing the row past its own dropdown's edge.
#
# The STORED row (what ChipPicker's matcher scores) still carries the
# FULL text out to :data:`PICKER_ROW_MAX` -- only the DRAWN copy is
# capped, at render time, by the widget -- same "trim by the widget, not
# by the formatter" rule v0.57.0 established, extended to a fixed-column
# shape instead of a flat ellipsize.

#: ``"25-08-24 14:32"`` -- belief_created_text's own fixed width (14) plus
#: one gutter column.
PICKER_STAMP_COL = 15
#: Widest realistic verdict this store produces, padded to. NOT a
#: computable ceiling -- v0.69.0 measured `proposal_verdict` against real
#: shapes and it is unbounded in principle (``memory/project:<slug>`` and
#: ``skill/<name>`` both carry FREE TEXT with no length cap of their own;
#: measured examples already run past this column on both fields, e.g.
#: "retract → memory/project:doxa-mode" at 34 and a real skill name from
#: this very store's own skill list, "add → skill/curate-deepseek-stdin-
#: launch", at 40). ``ellipsize`` already handles what does not fit, the
#: same way the claim/text column handles a claim longer than its own
#: cap -- so this stays a PRAGMATIC size, not an attempt at a true
#: maximum: LORE's own outcome words top out at "contradicted"/"never
#: tested" (12, so beliefs are never the constraint), and 28 covers every
#: `memory`/`user`, most `memory/project` and most `belief retract`
#: verdicts actually seen without truncation, at the cost of ellipsizing
#: the occasional long project slug or skill name -- which would need
#: truncating at any width short of "unbounded". Merging the two menus'
#: formatters (v0.67.0) did not change what proposals need, so this
#: number is unchanged by it and remains the size that decides the
#: column: sized for its wider user, same as before.
PICKER_STATUS_COL = 28
#: `_fmt_age`'s TRUE ceiling, measured (v0.69.0) rather than assumed: not
#: 5 as an earlier comment here claimed, but 6 -- `_fmt_age(86399)` reads
#: "23h59m", the sub-day branch's own widest case (a belief confirmed, or
#: a proposal staged, just under 24h ago -- a routine value, not an edge
#: case) and wider than anything the day-tier branches can produce short
#: of implausible multi-century ages. 6 + 1 gutter = 7: the NUMBER below
#: was already correct, only the reasoning written above it was not.
PICKER_AGE_COL = 7
#: The text column's own cap -- independent of the fixed columns before
#: it, per spec: ``min(100, terminal width)``.
PICKER_TEXT_CAP = 100
#: How many columns the fixed stamp/status/age prefix spends before the
#: text column starts -- callers that need to split a rendered row back
#: into "prefix" and "text" (ChipPicker's own render-time trim) read this
#: rather than re-deriving it from the three constants above.
PICKER_PREFIX_WIDTH = PICKER_STAMP_COL + PICKER_STATUS_COL + PICKER_AGE_COL


def lore_created_text(record: dict, *, now: "float | None" = None) -> str:
    """``YY-MM-DD HH:MM`` a belief or a staged proposal entered the store --
    the timestamp column both chip pickers lead with.

    A record-shape-agnostic rename of what :func:`belief_created_text`
    already computes: it only ever reads ``created``, and a proposal
    carries that field in the same ``lore_core.config.utcnow`` format a
    belief does (see :func:`_parse_lore_time`), so one function serves
    both rather than two copies of the same four lines drifting apart."""
    return belief_created_text(record, now=now)


def format_picker_prefix(stamp: str, status: str, age: str) -> str:
    """The fixed-width stamp/status/age columns every picker row in the
    shared shape leads with -- always exactly :data:`PICKER_PREFIX_WIDTH`
    columns, independent of terminal width. Padded, never omitted (a blank
    field still holds its column open) and ellipsized if a caller somehow
    hands one over-length, rather than letting it push the text column out
    of alignment with the row above and below it."""
    stamp_col = ellipsize(stamp or "", PICKER_STAMP_COL - 1).ljust(PICKER_STAMP_COL)
    status_col = ellipsize(status or "", PICKER_STATUS_COL - 1).ljust(PICKER_STATUS_COL)
    age_col = ellipsize(age or "", PICKER_AGE_COL - 1).ljust(PICKER_AGE_COL)
    return f"{stamp_col}{status_col}{age_col}"


def format_picker_column_header() -> str:
    """The one column-name header both LORE chip-picker menus show
    (v0.69.0), in the EXACT same fixed columns :func:`format_picker_prefix`
    gives every data row -- so it is built the same way, through the same
    function, rather than a second hand-aligned string that could drift
    from the columns it is supposed to name.

    ``status`` and ``text``, not ``verdict``/``claim``: the header is
    SHARED between the beliefs picker (where the column holds an outcome
    kind and the free text is a claim) and the proposals picker (where it
    holds a proposed verdict and the text is the proposal itself) -- a
    word that reads correctly for only one of the two menus would be
    wrong the other half of the time it is on screen.

    Callers prepend :attr:`ChipPicker.ROW_CHROME_COLS` worth of blank
    space themselves (the same three columns every data row spends on its
    own leading mark/selection glyph) -- this function only knows about
    the columns after that point, the ones :func:`format_picker_prefix`
    already owns."""
    return format_picker_prefix("date", "status", "age") + "text"


def format_picker_row(
    stamp: str, status: str, age: str, text: str, *, width: int,
) -> str:
    """The one row both the beliefs and proposals chip-picker menus render:
    ``YY-MM-DD HH:MM   status   age   text``, in fixed-width columns so
    neighbouring rows line up as a table rather than drifting with content
    length (the proposals row's own defect before this -- see this
    section's lead).

    ``width`` bounds the TEXT column only -- the fixed prefix before it is
    never cut. Two callers, two different meanings for it:

    * **Storage** (:func:`_fmt_belief_row` / :func:`_fmt_pending_row`, no
      ``width`` given by their own callers): ``width=PICKER_ROW_MAX``, i.e.
      as long as a claim or proposal body ever gets -- the string
      ChipPicker's matcher scores, so a word past what any screen could
      show is still findable. This function applies NO 100-column cap of
      its own; that cap is a DISPLAY decision, made once real geometry is
      known (see below), not a storage one.
    * **Display** (``ChipPicker._render_rows``, ``row_prefix_width`` set):
      the widget slices its own stored label at :data:`PICKER_PREFIX_WIDTH`
      and re-ellipsizes only the text half to ``min(PICKER_TEXT_CAP,
      measured_budget)`` -- literally the operator's spec -- without
      calling back into this function at all, since the prefix it already
      has is already correctly padded."""
    prefix = format_picker_prefix(stamp, status, age)
    shown_text = ellipsize(text, max(0, width))
    return f"{prefix}{shown_text}"


def _fmt_belief_row(belief: dict, *, width: int = PICKER_ROW_MAX) -> str:
    """One beliefs-picker row, in the shared :func:`format_picker_row`
    shape: when it was created, what reality last said about it (its
    OUTCOME KIND and the age of that verdict, as two separate fixed
    columns rather than one merged "confirmed 2d" string), then the claim.

    Returned against ``width=PICKER_ROW_MAX`` by default -- i.e. as long
    as :data:`PICKER_ROW_MAX` allows -- since v0.57.0 and trimmed to fit
    by the widget that paints it (v0.67.0 extends that trim to a
    fixed-column shape instead of a flat ellipsize; see
    :data:`PICKER_PREFIX_WIDTH`). That is not only about width: the filter
    scores this string, so a claim already cut here was a claim whose tail
    could not be searched for.

    Status and age are blank (padded, not omitted) for a belief with no
    outcome ledger at all -- the same "an absent key is an admission, a
    zero is a measurement" rule :func:`belief_outcome_kind` already
    follows, extended to the picker row so a belief arriving from an
    older daemon renders a blank column rather than a guessed one."""
    stamp = lore_created_text(belief)
    kind = belief_outcome_kind(belief)
    status = NEVER_TESTED if kind == "untested" else kind
    age = ""
    if kind in OUTCOME_EVENTS:
        secs = _age_of(belief.get("outcome_at"))
        age = _fmt_age(secs) if secs is not None else ""
    claim = _one_line(str(belief.get("claim") or ""), PICKER_ROW_MAX)
    return format_picker_row(stamp, status, age, claim, width=width)


def _pending_id_stamp(pid: str) -> str:
    """A proposal's own staged-at moment, recovered from its pending id
    when the record carries no ``created`` field of its own.

    Every pending id lore_core has ever minted -- ``lore_core.gate.
    stage_write``'s and ``lore_core.deriver``'s own staging ``put``, the
    two and only writers of ``pending/*.json`` -- is the SAME shape:
    ``<14-digit UTC timestamp>-<counter>``, the filename an id this
    proposal cannot exist without. ``created`` was added to the payload
    later; a proposal staged before that landed still has its moment, in
    its own id, and a stamp column that reads only ``created`` throws that
    away for exactly those rows -- blank where a real timestamp was
    recoverable the whole time.

    Not a guess: the id's digits ARE the clock the proposal was minted
    from, only not duplicated into the JSON body yet. ``""`` for anything
    that is not 14 digits followed by a dash -- a foreign or
    hand-authored id renders blank rather than a wrong date."""
    digits = str(pid or "").split("-", 1)[0]
    if len(digits) != 14 or not digits.isdigit():
        return ""
    from datetime import datetime, timezone

    try:
        parsed = datetime.strptime(digits, "%Y%m%d%H%M%S")
    except ValueError:
        return ""
    return parsed.replace(tzinfo=timezone.utc).strftime(LORE_TIME_FORMAT)


def _fmt_pending_row(item: "dict | str", *, width: int = PICKER_ROW_MAX) -> str:
    """One ``/pending`` row, in the shared :func:`format_picker_row` shape:
    when it was staged, WHAT APPROVING IT WOULD DO (the status column) and
    how long it has waited (the age column), then the proposal's own text.

    Item V's requirement in one line -- "a row that does not say what
    approving it changes is not reviewable". The verdict is now a FIXED
    column rather than leading a `` · ``-joined string, which is what lets
    it line up against the row below it instead of drifting with its own
    length; the text is still in the string because ChipPicker's
    type-to-filter matches this exact label and a row you cannot search by
    its own words is not a row.

    Accepts a bare string as well as a record: see :func:`as_proposal`.

    ``created`` first, :func:`_pending_id_stamp` as fallback -- stamp AND
    age both derive from the SAME resolved value, so a recovered stamp
    also recovers the wait beside it rather than leaving that column
    blank on its own."""
    proposal = as_proposal(item)
    created = str(proposal.get("created") or "").strip() or _pending_id_stamp(
        str(proposal.get("pid") or "")
    )
    stamp = lore_created_text({"created": created}) if created else ""
    status = proposal_verdict(proposal)
    secs = _age_of(created) if created else None
    age = _fmt_age(secs) if secs is not None else ""
    text = _one_line(proposal_text(proposal), 200)
    return format_picker_row(stamp, status, age, text, width=width)


# -- item V: timestamps, age, provenance, and the proposed verdict --------
#
# Four things the v0.27.0 picker never carried, first built for item V's
# beliefs browser (since removed, v0.69.0, in favour of putting all four
# on the picker's own row) -- and all four are pure functions of one
# record, so they live here beside the row formatters that already read
# those records rather than inside whatever widget paints them.
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

    THE YEAR IS ALWAYS THERE, in two digits. v0.48.0 dropped it from a
    belief derived in the current year to buy back a column, on the
    reasoning that a year the reader is standing in says little. The user
    asked for it back and wrote the format out: ``YY-MM-DD``. The second
    reason is better than the first was -- a stamp that is 11 characters
    for some rows and 14 for others makes the CLAIM column start in a
    different place down the list, which is exactly the shifting surface
    this codebase avoids everywhere else. Fixed width beats one column.

    The column it costs is no longer taken from the claim either: since
    v0.57.0 a picker row is trimmed by the WIDGET against its own measured
    width rather than by this function against a constant.

    ``full=True`` spells the century out (``YYYY-MM-DD HH:MM``) -- built
    for the now-removed beliefs browser's own rows, which were read as a
    record rather than as a glance and had the width for it; no picker
    caller sets it today, but the form stays (and stays tested) rather
    than being ripped out from under whatever next wants a record-length
    stamp. Both forms are fixed-width, both carry HH:MM, and neither has
    ever carried seconds -- a precision nobody acts on for a claim a
    background reviewer derived."""
    stamp = str(belief.get("created") or "").strip()
    when = _parse_lore_time(stamp)
    if when is None:
        # Unparseable but present: show whatever date-shaped prefix it has
        # rather than dropping the column, and never invent a clock time
        # for a string this function could not read.
        return stamp[:10]
    import time as _time

    parts = _time.gmtime(when)
    return _time.strftime("%Y-%m-%d %H:%M" if full else "%y-%m-%d %H:%M", parts)


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
    means the tested ones are needles, and a picker that interleaves them
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


def belief_evidence_rows(rows: "list[dict]") -> "list[str]":
    """One belief's derivation trail as PICKER ROWS -- one entry per
    evidence event (session, project, note), each meant to become its OWN
    disabled row beneath the expanded belief.

    v0.69.0 removed the beliefs browser's ``EvidenceTrail`` widget (which
    mounted the whole trail as one Static under the row) in favour of
    expanding a belief's evidence in place on the chip picker itself:
    Right on a highlighted row inserts these as real rows directly under
    it -- disabled, so the highlight (and every action key) still lands
    only on the belief, never on its own evidence -- and Left removes
    them again. A LIST, not one joined blob, because the picker already
    has the machinery for "many rows belong to one thing" (the fold
    header's own child rows) and reusing it is simpler than inventing a
    second shape for one blob widget to hold.

    NEVER EMPTY: a belief with no evidence at all still gets ONE row
    saying so -- ``[]`` would render as nothing, and "no evidence" and
    "not fetched yet" (the picker's own transient "loading" row, painted
    before this function is even called) have to look different on
    screen, not merely be different internally.

    The fetch itself is unchanged: still lazy, still one belief at a time,
    still capped (see ``SessionEngine.belief_evidence`` /
    :data:`BELIEF_EVIDENCE_LIMIT`) -- a picker over hundreds of beliefs
    must not pull hundreds of trails just because the list is open, and a
    trail past the cap says so in its own trailing row rather than being
    shown as if it were complete."""
    if not rows:
        return [
            "    no evidence rows — this belief carries no derivation "
            "trail in the store"
        ]
    out: "list[str]" = []
    for row in rows:
        when = str(row.get("created") or "?")
        session = str(row.get("session_id") or "?")
        project = str(row.get("project") or "")
        note = _one_line(str(row.get("note") or ""), 200)
        head = f"    {when}  session {session}"
        if project:
            head += f"  [{project}]"
        text = _escape_markup(head)
        if note:
            text += "\n" + _escape_markup(f"        {note}")
        if row.get("note_truncated"):
            text += "\n        [note truncated — larger than one wire frame]"
        out.append(text)
    if rows[-1].get("trail_truncated"):
        # Set on the LAST row DOXA actually read (SessionEngine.
        # belief_evidence fetches limit+1 to detect this) -- its OWN
        # trailing row, never appended onto the last citation's text, so
        # the honesty note can never be mistaken for part of that
        # citation.
        out.append(
            "    … trail continues — more evidence than one page holds"
        )
    return out


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
    channel = BELIEF_CHANNEL_RULE.get(subject)
    if channel:
        # The rule in full, not just the two-word tag the scope label
        # already carries -- a hover is where "authorizes nothing" (the
        # consequence a user-model belief's own confidence number cannot
        # convey) actually gets said.
        lines.append(channel)
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


def _action_name(action: str) -> str:
    """``focus_group(3)`` -> ``focus_group``. A key family bound to one
    action with different arguments (v0.97.0's Ctrl+1..Ctrl+9) shares a
    door with the member that IS a command's ``binding`` -- Ctrl+1 is
    /pane's -- and comparing the raw strings would miss every other
    digit, leaving eight keys named in the startup notice with nothing
    to press instead."""
    return action.split("(", 1)[0].strip()


def _door_for(key: str) -> str:
    """The slash command that reaches the same place `key` does, read off
    the SAME registry /help renders -- never guessed, never hand-copied.

    Two ways in: `key` is a command's own ``binding`` (``ctrl+comma`` ->
    ``/settings``), or `key` shares its Textual ACTION with a different
    key that IS a command's binding -- ``ctrl+tab`` and ``shift+tab`` both
    fire ``cycle_permission_mode``, only the latter is ``/mode``'s
    ``binding``, and Ctrl+Tab riding beside Shift+Tab (see
    ``DoxaApp.BINDINGS``) is exactly the case this second pass exists for.
    Empty when neither turns up anything -- a bare hotkey with no slash
    form at all, which stays true and simply names no door."""
    for cmd in commands_mod.REGISTRY:
        if cmd.binding == key:
            return cmd.name
    # Deferred, same reason app_bindings() defers it: doxa.app imports
    # this package, so the arrow only points back at call time.
    from ..app import DoxaApp

    def _key_action(binding: Any) -> "tuple[str, str]":
        if isinstance(binding, tuple):
            return binding[0], binding[1]
        return binding.key, binding.action

    action = ""
    for binding in DoxaApp.BINDINGS:
        raw_key, raw_action = _key_action(binding)
        if raw_key == key:
            action = raw_action
            break
    if not action:
        return ""
    for binding in DoxaApp.BINDINGS:
        raw_key, raw_action = _key_action(binding)
        # Compare the action NAME, not the raw string: v0.97.0's
        # Ctrl+1..Ctrl+9 are one action taking the group number, so
        # `focus_group(7)` must still find `focus_group(1)` -- which IS
        # /pane's binding -- or eight of the nine name no door at all.
        if _action_name(raw_action) != _action_name(action) or raw_key == key:
            continue
        for cmd in commands_mod.REGISTRY:
            if cmd.binding == raw_key:
                return cmd.name
    return ""


def unreachable_doors() -> "list[tuple[str, str]]":
    """``(pretty key, door command)`` for every advertised binding this
    terminal cannot deliver -- the command bindings and the bare hotkeys
    both, read off the same two sources /help renders. The door is ``""``
    when nothing in the registry names one. Empty on a terminal that
    grants the kitty protocol AND on one we never got to measure.

    :func:`unreachable_bindings` is this with the doors dropped; the
    startup notice (:func:`unreachable_notice`) is this with them kept."""
    keys = [cmd.binding for cmd in commands_mod.REGISTRY if cmd.binding]
    keys += [key for key, _description in app_bindings()]
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        if _binding_mark(key):
            out.append((_pretty_key(key), _door_for(key)))
    return out


def unreachable_bindings() -> "list[str]":
    """Every advertised binding this terminal cannot deliver, in pretty
    form (``["Ctrl+,"]``) -- the command bindings and the bare hotkeys
    both, read off the same two sources /help renders. Empty on a terminal
    that grants the kitty protocol AND on one we never got to measure.

    ``/doctor`` names these; /help marks them in place."""
    return [pretty for pretty, _door in unreachable_doors()]


# Above this many affected bindings, the startup notice stops naming them
# one by one and names the count instead -- one line, not a lecture. Two
# is the every-day case (Ctrl+, / Ctrl+Tab, see tests/test_keyboard.py)
# and reads fine spelled out; a terminal missing enough of the kitty
# protocol to lose a handful of bindings gets a pointer at /doctor's full
# table instead of a run-on sentence.
NOTICE_SUMMARY_THRESHOLD = 3


def _doors_worth_naming() -> "list[tuple[str, str]]":
    """:func:`unreachable_doors`, minus every key whose ACTION some other
    binding still reaches on this terminal.

    v0.95.0 moved the split and diff keys to Ctrl+O / Ctrl+N / F2 and kept
    Alt+S / Alt+D / Alt+G beside them as kitty-tier aliases. All three
    aliases are undeliverable under the legacy encoding and belong in
    /doctor's table -- but naming them in a STARTUP notice would tell the
    user about a loss they do not have: the action is one keystroke away
    on a key that works. What earns the interruption is a key with no
    reachable spelling at all (``Ctrl+,`` for /settings), where the door
    really is the only way in.

    Falls back to naming everything if the action map cannot be read --
    an over-full notice is a smaller failure than a silent one."""
    doors = unreachable_doors()
    try:
        from ..app import DoxaApp
        by_action: "dict[str, list[str]]" = {}
        for binding in DoxaApp.BINDINGS:
            key = getattr(binding, "key", None)
            action = getattr(binding, "action", None)
            if key and action:
                by_action.setdefault(action, []).append(key)
        reachable_actions = {
            action for action, keys in by_action.items()
            if any(not _binding_mark(k) for k in keys)
        }
        dead_but_covered = {
            _pretty_key(key)
            for action in reachable_actions
            for key in by_action[action]
            if _binding_mark(key)
        }
    except Exception:  # noqa: BLE001 -- see docstring
        return doors
    return [(pretty, door) for pretty, door in doors
            if pretty not in dead_but_covered]


def _collapse_families(doors: "list[tuple[str, str]]") -> "list[tuple[str, str]]":
    """Fold a run of keys sharing one door into a single entry.

    v0.97.0 binds Ctrl+1..Ctrl+9 to one action taking the group number,
    so a legacy terminal loses NINE keys that are one gesture. Listing
    them singly pushed the notice past its summary threshold and turned a
    line the user could act on ("Ctrl+, -- use /settings") into a count
    they could not ("10 bound keys, run /doctor"). What matters is how
    many THINGS are unreachable, not how many keycaps."""
    out: "list[tuple[str, str]]" = []
    seen_doors: "dict[str, int]" = {}
    for key, door in doors:
        if door and door in seen_doors:
            first, _ = out[seen_doors[door]]
            head = first.split("\u2013")[0]
            out[seen_doors[door]] = (f"{head}\u2013{key.split('+')[-1]}", door)
            continue
        if door:
            seen_doors[door] = len(out)
        out.append((key, door))
    return out


def unreachable_notice() -> str:
    """One line for a session's opening block: which bound keys THIS
    terminal cannot deliver, and the slash command that reaches them
    instead -- or ``""`` when there is nothing to say.

    Empty in exactly the two cases :func:`unreachable_bindings` is empty
    in: a terminal that grants the kitty protocol (nothing is lost), AND
    one this process never got to measure (``doxa.keyboard.UNKNOWN``).
    The second one is deliberate, not an oversight -- this notice's whole
    claim is "these specific keys are dead", and UNKNOWN means we have NO
    evidence for that claim. Firing it anyway on every boot of a slow SSH
    hop or a multiplexer that ate the probe's reply would be exactly the
    false alarm ``doxa.keyboard``'s module docstring exists to prevent,
    on the loud side instead of the silent one. A user who wants to know
    "did DOXA even measure my terminal" already has that answer, on
    request rather than by interruption: ``/doctor``'s keyboard-enhancement
    row states "not measured" outright (doxa.doctor._keyboard_enhancement_check)."""
    doors = _collapse_families(_doors_worth_naming())
    if not doors:
        return ""
    count = len(doors)
    if count > NOTICE_SUMMARY_THRESHOLD:
        return (
            f"this terminal can't deliver {count} bound keys -- "
            "run /doctor for the full list"
        )
    plural = "key" if count == 1 else "keys"
    named = ", ".join(
        f"{key} (use {door})" if door else key for key, door in doors
    )
    return (
        f"this terminal can't deliver {count} bound {plural}: {named} "
        "-- see /doctor for details"
    )


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
    and (v0.56.0) every row of the ``/search`` popup.

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
    wt_chars = breakdown.get("worktree_notice_chars")
    if isinstance(wt_chars, int):
        lines.append(
            f"worktree notice: {wt_chars:,} characters appended to the "
            "system prompt at connect (this session runs in its own git "
            "worktree). Also counted INSIDE the system-prompt row above, "
            "for the same reason as the lore snapshot."
        )
    ga_chars = breakdown.get("graph_awareness_chars")
    if isinstance(ga_chars, int):
        lines.append(
            f"belief graph notice: {ga_chars:,} characters appended to the "
            "system prompt at connect (this store carries at least one "
            "typed relation between two active beliefs, so the session is "
            "told lore_belief_neighbours exists). Also counted INSIDE the "
            "system-prompt row above, for the same reason as the lore "
            "snapshot."
        )
    gc_chars = breakdown.get("graph_context_chars")
    if isinstance(gc_chars, int):
        lines.append(
            f"graph context: {gc_chars:,} characters added to the LAST "
            "turn's prompt (LORE's graph-backed belief context, "
            "DOXA_GRAPH_CONTEXT). Unlike the three rows above, this rides "
            "the per-turn additionalContext path, not the connect-time "
            "system prompt -- the CLI counts its tokens correctly on its "
            "own, inside the categories above, once that turn's usage "
            "comes back. Shown here as the last known size, since it "
            "varies by prompt rather than being fixed for the session."
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


# -- /context (item K, continued): the grid -----------------------------
#
# v0.75.0's ask was "block art instead of the numbers", answered with a
# single proportional bar of ``█``. This redesign's ask is more specific:
# make it look like Claude Code's OWN ``/context`` -- a fixed 10x20 grid of
# 200 cells (0.5% each), model and headline beside the top rows, a
# per-category legend beside the lower rows. Read the same way v0.75.0's
# ask was read: "instead of the numbers" means LEADS the numbers, not
# replaces them. context_breakdown_text (the numbers) is UNTOUCHED by
# everything below -- :class:`doxa.ui.transcript.ContextBlock` stacks the
# grid above it, exactly where the bar used to sit.
#
# **The grid does not stretch.** The old bar was proportional to whatever
# width the pane happened to have (24 to 60 columns); Claude Code's grid is
# always the same 200 cells regardless of terminal width, because the
# CELLS are the unit of resolution, not the pane. So there is no
# "narrower bar" degrade step here the way there was for ``█`` -- either
# the pane has room for GRID_COLUMNS * GRID_CELL_WIDTH columns and the
# grid draws at its one true size, or it does not and /context shows the
# numbers alone, the same final degrade state the old bar had.
#
# **Two cell styles, one geometry, one honesty rule.** The owner asked for
# Claude Code's own draughts glyphs (⛀ U+26C0, ⛁ U+26C1, ⛶ U+26F6) --
# Miscellaneous Symbols, the same font-coverage class as the Geometric
# Shapes triangles v0.58.0's banner work rejected for tofu risk. Claude
# Code ships them regardless, so they are the PRIMARY style; DOXA_CONTEXT_
# GRID/config.py's ``context_grid`` setting adds ``ascii`` ([#]/[ ]) as a
# manual fallback for a font that tofu's them, because nothing in a
# terminal reports its own glyph coverage -- DOXA cannot detect this, only
# offer the switch and let a user who SEES tofu flip it once. Both styles
# read the identical 200 measured cells (:func:`context_grid_cells`); only
# the two characters differ, and GRID_CELL_WIDTH is sized to the WIDER of
# the two forms (both are exactly 3 columns: `` c `` or ``[c]``) so
# switching the setting changes nothing about the grid's width or the
# side panel's start column.
#
# **The two "used" glyphs are texture, not two categories.** Claude Code's
# own grid alternates ⛀/⛁ within a single color's run -- read here as a
# checkerboard dither on the FILLED cells for visual texture, exactly
# mirrored by the ascii style having only one used-glyph (``#``): a used
# cell says "this category" through its COLOR and its POSITION in the
# grid, never through which of the two used-glyphs it drew. Confirmed by
# the owner's own ascii sketch, which has no second used-symbol to
# alternate with.
#
# **Colour is load-bearing, not decorative, once ASCII is in play.** On a
# style/terminal combination with no color (or a user who cannot
# distinguish it), the grid's category boundaries collapse to a wall of
# identical ``[#]`` -- the SAME trade the ctx% chip already makes for its
# escalation color, and why the legend beside the grid, and the full
# numbers beneath it, carry every figure the grid shows in text too:
# nothing on this screen is available ONLY as color.
#
# **The colors are reused, not invented.** Every hex literal below already
# paints something else in theme.tcss, unchanged from the bar this grid
# replaces: PROVIDER_GLYPH_COLOR is the accent every clickable chip already
# wears, the other four are the ``#session-tabs`` status ladder
# (``-done-unseen``, ``-staged``, ``-working``) plus SystemBlock's own left
# rule. Keyed by CATEGORY NAME now rather than list position (the grid's
# legend has to label a color, so the mapping has to survive the CLI
# reordering its own list); an unrecognized category name still gets a
# color, cycling the same five hex values by its position in the list, the
# bar's old fallback behavior kept for exactly the categories that were
# never named here.
GRID_COLUMNS = 10
GRID_ROWS = 20
GRID_CELLS = GRID_COLUMNS * GRID_ROWS  # 200 -- 0.5% of the window each
GRID_CELL_WIDTH = 3  # " c " (glyphs) and "[c]" (ascii) are both 3 columns
GRID_GUTTER = 2  # columns between the grid and the side panel
GRID_PANEL_MIN_COLUMNS = 20
"""Below this the side panel would be truncated past legibility -- the
grid still draws at its one true size, but model/headline/legend drop
out entirely rather than ship a panel too narrow to read. Nothing is
LOST: every figure the panel would have shown is still in
context_breakdown_text, one screen below, unchanged."""

GRID_GLYPH_USED: "tuple[str, str]" = ("⛀", "⛁")
GRID_GLYPH_FREE = "⛶"
GRID_ASCII_USED = "#"
GRID_ASCII_FREE = " "

CONTEXT_GRID_TRACK = "#3A3429"
"""theme.tcss's own border grey -- the top rung of the surface ramp, used
everywhere else in this app to mean "a boundary, not content." The grid
reuses it (the old bar's CONTEXT_BAR_TRACK, renamed) for every free cell:
free space is not a component competing for a color, it is the absence of
one, and drawing it in a content color would read as tokens nobody spent."""

CONTEXT_GRID_FREE_NAMES = frozenset({"free space"})
"""The one category name (matched case-insensitively, exactly -- no
substring, no near-miss) this module treats as the window's own unspent
remainder rather than a component. Narrow on purpose: an SDK that ever
renames or drops this category degrades to "one more colored cell", never
to a silently mislabeled one."""

CONTEXT_GRID_CATEGORY_COLORS: "dict[str, str]" = {
    "system prompt": PROVIDER_GLYPH_COLOR,  # "#D97757" -- the app's own accent
    "system tools": "#6FCF97",              # #session-tabs Tab.-done-unseen
    "mcp tools": "#A98FD1",                 # #session-tabs Tab.-staged
    "memory files": "#E0A83C",              # #session-tabs Tab.-working
    "messages": "#7A9B6E",                  # SystemBlock's own left rule
}
"""Lowercased category name -> color, for the five names ``get_context_
usage`` actually sends (:data:`tests.test_context.CTX_USAGE`, a realistic
reply shaped exactly like the SDK's own). Stable across a CLI that
reorders its own ``categories`` list -- unlike the old bar's
position-keyed palette, the grid's legend has a name to label, and a name
that always wears the same color is the whole point of a legend."""

CONTEXT_GRID_PALETTE: "tuple[str, ...]" = tuple(CONTEXT_GRID_CATEGORY_COLORS.values())
"""Fallback cycle, by the category's own position in the CLI's list, for
any name not in :data:`CONTEXT_GRID_CATEGORY_COLORS` -- the exact values
the old bar used unconditionally, kept here for the one case (an SDK
category this module has never seen) where there is no name to key on."""


def context_grid_mode() -> str:
    """``"ascii"`` or ``"glyphs"`` -- config.py's ``context_grid`` setting
    (``DOXA_CONTEXT_GRID``), read the same way :func:`git_branch_symbol`
    reads its own font-coverage switch: DOXA cannot probe what a terminal's
    font covers, so this is the user's manual answer, not a detection this
    function performs. Anything other than the literal ``"ascii"`` --
    unset, ``"glyphs"``, or a stray value -- reads as glyphs, Claude Code's
    own look and this module's default."""
    return "ascii" if config_mod.raw("DOXA_CONTEXT_GRID").strip().lower() == "ascii" else "glyphs"


def _context_grid_color(name: str, index: int) -> str:
    key = name.strip().lower()
    if key in CONTEXT_GRID_FREE_NAMES:
        return CONTEXT_GRID_TRACK
    known = CONTEXT_GRID_CATEGORY_COLORS.get(key)
    if known is not None:
        return known
    return CONTEXT_GRID_PALETTE[index % len(CONTEXT_GRID_PALETTE)]


def context_grid_cells(breakdown: "dict | None") -> "list[tuple[str, str]] | None":
    """``[(category_name, color), ...]``, exactly :data:`GRID_CELLS` (200)
    entries, row-major (index 0 is the grid's top-left cell, index
    ``GRID_COLUMNS - 1`` ends row 0) -- the grid's own half of
    :func:`context_breakdown`'s discipline: every cell traces to a
    measured category, nothing is estimated, and this returns ``None``
    rather than a grid drawn against a guessed denominator.

    ``None`` when there is nothing honest to draw: no breakdown, no
    reported window (``max_tokens`` -- a limit the CLI never sent reads
    ``?`` and stays ``?``; there is no denominator to be proportional
    against, so there is no grid, full stop -- unlike the old bar this
    never degrades to a narrower shape, because the grid has no narrower
    shape), or no measured categories.

    Cell counts are assigned by FLOORING each category's CUMULATIVE share
    of the 200 cells and taking the difference from the previous
    category's floored position -- not rounding, and not independently per
    category. Floor over round: a category that has not YET earned a
    whole cell must not show one -- rounding a component sitting at 0.9 of
    a cell's width up to a full filled cell would be exactly the kind of
    small lie item K's own docstring rules out for the numbers, just
    committed one layer up in the picture instead of in an integer. Floor
    also cannot overshoot the grid's own 200-cell width the way
    independent per-category rounding could (v0.70.0's boot-mark lesson,
    already paid for once by the old bar's cumulative-ROUND scheme): the
    running position is monotonic and clamped to ``[0, GRID_CELLS]`` by
    construction, so the sum of every count this returns, plus its own
    trailing remainder, is always exactly 200, never more. The remainder
    -- whatever cumulative rounding dust is left once every category has
    drawn its share -- is appended as more free cells, so the grid always
    reads as content followed by however much window is genuinely unspent,
    the same rule the old bar's own trailing track kept.

    The category named "free space" (:data:`CONTEXT_GRID_FREE_NAMES`)
    draws as free cells (empty name, :data:`CONTEXT_GRID_TRACK`) rather
    than a content color, wherever it falls in the CLI's own list."""
    if not breakdown:
        return None
    window = breakdown.get("max_tokens")
    if not isinstance(window, (int, float)) or window <= 0:
        return None
    categories = breakdown.get("categories")
    if not isinstance(categories, list) or not categories:
        return None

    cells: list[tuple[str, str]] = []
    cumulative_share = 0.0
    prev_pos = 0
    for idx, row in enumerate(categories):
        tokens = row.get("tokens")
        if not isinstance(tokens, (int, float)):
            continue  # no number, no cell -- same omission rule as the text
        name = str(row.get("name") or "").strip()
        cumulative_share = min(1.0, cumulative_share + float(tokens) / float(window))
        pos = int(cumulative_share * GRID_CELLS)  # floor -- see docstring above
        count = pos - prev_pos
        prev_pos = pos
        if count <= 0:
            continue
        is_free = name.lower() in CONTEXT_GRID_FREE_NAMES
        label = "" if is_free else name
        color = CONTEXT_GRID_TRACK if is_free else _context_grid_color(name, idx)
        cells.extend([(label, color)] * count)

    remainder = GRID_CELLS - prev_pos
    if remainder > 0:
        cells.extend([("", CONTEXT_GRID_TRACK)] * remainder)
    return cells or None


def _grid_cell_glyph(style: str, *, free: bool, parity: int) -> str:
    if style == "ascii":
        return GRID_ASCII_FREE if free else GRID_ASCII_USED
    if free:
        return GRID_GLYPH_FREE
    return GRID_GLYPH_USED[parity % len(GRID_GLYPH_USED)]


def _grid_cell_markup(style: str, color: str, *, free: bool, parity: int) -> str:
    glyph = _grid_cell_glyph(style, free=free, parity=parity)
    # The ascii style's own cell shape, "[#]"/"[ ]", is a literal pair of
    # square brackets -- exactly what Rich/Textual markup syntax also uses
    # for a tag, and "[#]" in particular (a bracket, a bare "#", zero
    # trailing hex digits) parses as a validly-shaped, if empty, color
    # tag. Left unescaped, DOXA's own markup parser would eat the cell's
    # visible brackets the same way an unescaped "[" in a model-chosen
    # description could swallow a click target (_escape_markup's own
    # docstring) -- so the body is escaped the identical way before it is
    # ever wrapped in the real color span around it.
    body = _escape_markup(f"[{glyph}]") if style == "ascii" else f" {glyph} "
    paint = CONTEXT_GRID_TRACK if free else color
    return f"[{paint}]{body}[/]"


def _context_grid_panel(breakdown: dict, style: str) -> "list[str]":
    """Plain-text lines for beside the grid: the model beside the TOP
    rows, then a blank, then the headline, then the category legend beside
    the LOWER rows -- Claude Code's own layout. Every figure here is a
    SECOND view of a number :func:`context_breakdown_text` already prints
    below; nothing is computed that function does not already carry."""
    lines: list[str] = []
    model = breakdown.get("model")
    if model:
        lines.append(short_model(str(model)))  # the same tier word tab labels use
        lines.append(str(model))
    total = breakdown.get("total_tokens")
    window = breakdown.get("max_tokens")
    percent = breakdown.get("percentage")
    lines.append("")
    if isinstance(total, (int, float)) and isinstance(window, (int, float)):
        pct = f"  ({float(percent):.1f}%)" if isinstance(percent, (int, float)) else ""
        lines.append(f"{fmt_tokens(int(total))}/{fmt_tokens(int(window))} tokens{pct}")
    categories = breakdown.get("categories")
    rows = [row for row in (categories or []) if isinstance(row.get("tokens"), (int, float))]
    if rows:
        lines.append("")
        # Not "Estimated usage by category" -- Claude Code's own heading --
        # because DOXA does not estimate: every figure below is the CLI's
        # own measurement, restated, and the word "estimated" would be a
        # claim about this screen that is simply false (item K's third
        # honesty rule, in :func:`context_breakdown_text`'s own docstring).
        lines.append("Usage by category")
        for idx, row in enumerate(categories):
            tokens = row.get("tokens")
            if not isinstance(tokens, (int, float)):
                continue
            name = str(row.get("name") or "?")
            is_free = name.strip().lower() in CONTEXT_GRID_FREE_NAMES
            color = CONTEXT_GRID_TRACK if is_free else _context_grid_color(name, idx)
            swatch = _grid_cell_markup(style, color, free=is_free, parity=0)
            share = (
                f"  ({float(tokens) / float(window) * 100:.1f}%)"
                if isinstance(window, (int, float)) and window else ""
            )
            lines.append(f"{swatch} {name}: {fmt_tokens(int(tokens))} tokens{share}")
    return lines


def context_grid_lines(
    breakdown: "dict | None", width: int, *, mode: "str | None" = None
) -> "list[str]":
    """The rendered grid, one markup row per string -- the side panel
    (model/headline/legend) appended after :data:`GRID_GUTTER` spaces on
    each row when ``width`` has room for it, omitted (grid alone) when it
    does not. ``[]`` -- the numbers-only degrade -- when
    :func:`context_grid_cells` has nothing to draw, or when ``width`` is
    narrower than the grid's own fixed geometry
    (``GRID_COLUMNS * GRID_CELL_WIDTH`` columns): unlike the old
    proportional bar this grid never draws smaller, only at its one true
    size or not at all.

    ``mode`` overrides :func:`context_grid_mode` for a caller that already
    knows the style (tests); a live caller leaves it ``None`` and gets the
    user's own setting."""
    cells = context_grid_cells(breakdown)
    if cells is None:
        return []
    grid_width = GRID_COLUMNS * GRID_CELL_WIDTH
    if width < grid_width:
        return []
    style = mode if mode in ("glyphs", "ascii") else context_grid_mode()

    grid_rows: list[str] = []
    for r in range(GRID_ROWS):
        parts = []
        for c in range(GRID_COLUMNS):
            name, color = cells[r * GRID_COLUMNS + c]
            parts.append(
                _grid_cell_markup(style, color, free=not name, parity=(r + c))
            )
        grid_rows.append("".join(parts))

    available_for_panel = width - grid_width - GRID_GUTTER
    if available_for_panel < GRID_PANEL_MIN_COLUMNS:
        return grid_rows
    panel_lines = _context_grid_panel(breakdown, style)
    out: list[str] = []
    for i, grid_row in enumerate(grid_rows):
        side = panel_lines[i] if i < len(panel_lines) else ""
        if side:
            out.append(f"{grid_row}{' ' * GRID_GUTTER}{ellipsize(side, available_for_panel)}")
        else:
            out.append(grid_row)
    return out


def context_grid_text(
    breakdown: "dict | None", width: int, *, mode: "str | None" = None
) -> str:
    """:func:`context_grid_lines`, joined -- or ``""`` when there is
    nothing to draw. The single call :class:`doxa.ui.transcript.ContextBlock`
    splices in where the old bar used to sit."""
    lines = context_grid_lines(breakdown, width, mode=mode)
    return "\n".join(lines)


def context_sources_text(breakdown: "dict | None") -> str:
    """The per-source summary Claude Code prints below its own grid --
    ``MCP tools · /mcp (loaded on-demand)`` / ``└ 202 tools · 0 tokens``,
    and the like -- as three sections, each hide-at-zero, each a SUMMARY
    of a figure this codebase already has, never a new measurement:

    * MCP tools, grouped by server, off the same ``mcp_tools`` rows
      :func:`context_breakdown` already normalizes (which includes DOXA's
      own in-process SDK MCP server -- whatever ``get_context_usage``
      names it comes through here like any other server).
    * Agents, off ``agents`` -- a real ``get_context_usage`` field
      (subagent definitions loaded into the window) that no ``/context``
      surface before this normalized at all; see
      :func:`doxa.engine.context_breakdown`'s own docstring.
    * Skills, off ``adopted_skills``/``adopted_skill_plugins`` -- the ONE
      figure here DOXA measures itself rather than reading off the CLI,
      because ``get_context_usage`` has no ``skills`` field to read at
      all (:func:`doxa.claude_plugins.adopted_skill_summary`). Absent
      entirely -- not a zero-count section -- when plugin adoption is off,
      the same hide-at-zero rule the LORE snapshot line and the worktree
      notice line already apply to a DOXA-contributed figure with nothing
      to report.

    ``""`` when none of the three has anything to say."""
    if not breakdown:
        return ""
    sections: list[str] = []
    mcp_tools = breakdown.get("mcp_tools")
    if isinstance(mcp_tools, list) and mcp_tools:
        by_server: "dict[str, list[int]]" = {}
        for row in mcp_tools:
            tokens = row.get("tokens")
            by_server.setdefault(str(row.get("server") or "?"), []).append(
                int(tokens) if isinstance(tokens, (int, float)) else 0
            )
        sections.append("MCP tools")
        for server, tok_list in by_server.items():
            sections.append(f"  └ {server}: {len(tok_list)} tools · {sum(tok_list):,} tokens")
    agents = breakdown.get("agents")
    if isinstance(agents, list) and agents:
        total = sum(
            int(row["tokens"]) for row in agents if isinstance(row.get("tokens"), (int, float))
        )
        sections.append("Agents")
        sections.append(f"  └ {len(agents)} agents · {total:,} tokens")
    skills = breakdown.get("adopted_skills")
    if isinstance(skills, int) and skills > 0:
        plugins = breakdown.get("adopted_skill_plugins") or 0
        sections.append("Skills · adopted plugins")
        sections.append(
            f"  └ {skills} skills from {plugins} plugin{'' if plugins == 1 else 's'}"
        )
    return "\n".join(sections)


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


#: Scoped staged-proposal counts, keyed by project slug and validated by
#: the pending directory's own mtime. Same shape and same reason as
#: :data:`_MEM_FILL_CACHE` above.
_STAGED_CACHE: "dict[str, tuple[float, int]]" = {}


def staged_count(slug: "str | None") -> "int | None":
    """How many staged proposals this project's reviews can see, or None
    when the staging area cannot be read at all.

    SCOPED, and it agrees with the list clicking the chip opens BY
    CONSTRUCTION: both walk ``lore_core.pending.load_pending`` through the
    same predicate, :func:`doxa.engine.pending_visible`. A chip reading 175
    over a picker showing 40 is worse than no chip, and the first version
    of this function was exactly that bug -- it counted
    ``lore_core.deriver.pending_texts``, which returns
    ``item["text"] or item["name"]`` and silently drops anything carrying
    neither, so a live spool of 59 (54 of them filemap proposals, which
    carry a path and a purpose and neither of those fields) rendered a chip
    reading 5.

    CACHED ON THE DIRECTORY'S MTIME, and that is the whole cost argument.
    ``_refresh_status`` runs on every event-driven refresh and already pays
    a belief ``COUNT(*)``; scoping requires opening each staged file. A
    directory's mtime changes when an entry is added or removed -- which is
    exactly when this count can change -- so an unchanged staging area
    costs ONE stat and a dict lookup. Editing a file in place bumps
    neither the mtime nor the count.

    Read locally rather than over the socket, the same way
    :func:`memory_fill` reads the memory file the daemon also writes: the
    LORE store is one directory shared by every process on the machine, and
    putting this in the status payload would move the same opens into the
    daemon rather than remove them."""
    try:
        import lore_core
        from lore_core.pending import load_pending

        from ..engine import pending_visible

        pdir = lore_core.ROOT / "pending"
        stamp = pdir.stat().st_mtime
        key = str(slug or "")
        hit = _STAGED_CACHE.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        count = sum(1 for _pid, item in load_pending()
                    if isinstance(item, dict) and pending_visible(item, slug))
        _STAGED_CACHE[key] = (stamp, count)
        return count
    except Exception:
        # No store, no pending directory, an older lore_core: the chip does
        # not appear. Same posture as memory_fill -- a convenience is never
        # a reason to degrade the status bar.
        return None


def staged_chip(count: "int | None") -> "tuple[str, str] | None":
    """(chip text, hint) for the staged-proposal count, or None to omit.

    HIDDEN AT ZERO, the convention the subagent and peer chips already
    follow: the status line is the most contended row in the UI and a chip
    reading `0 proposals` is a permanent reminder of nothing. An empty
    staging area is the ordinary state and needs no pixels."""
    if not count:
        return None
    noun = "proposal" if count == 1 else "proposals"
    return (
        f"{count} {noun}",
        f"{count} staged {noun} waiting for your approval -- click to "
        "review them one at a time, with what each would do if approved",
    )


def proposal_group_label(item: "dict | str") -> str:
    """Which fold a staged proposal falls under in the proposals picker.

    BY KIND, not by project or by age, because kind is what the VERDICT
    acts on: `memory/user` and `memory/project` write different files with
    different caps, `filemap` writes a path map, `belief` writes a SQL row,
    and a `skill` writes an executable ``SKILL.md`` into the agent's own
    skill directory. A reviewer decides about one of those at a time.

    The SKILL lane being its own group is the one non-obvious consequence
    and it is deliberate: LORE's own `/lore:pending` keeps skills out of
    memory clustering ("Skills are never clustered -- they stay their own
    lane") precisely because judging an installable script with the same
    glance as a remembered sentence is how a bad skill gets in. Grouping by
    kind gives them that lane without a special case.

    Derived from lore_core's own record fields rather than a hardcoded
    list, so a kind LORE starts writing gets its own fold instead of being
    filed silently under something else."""
    proposal = as_proposal(item)
    kind = str(proposal.get("kind") or "").strip().lower()
    if kind == "memory":
        scope = str(proposal.get("scope") or "").strip()
        return f"memory/{scope}" if scope else "memory"
    if kind:
        return kind
    # No kind: the deriver writes skill proposals with a `name` and no
    # kind (see cmd_pending's own final else), and an older daemon can
    # still send a bare string.
    if proposal.get("name"):
        return "skill"
    return "unknown"
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

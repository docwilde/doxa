"""doxa.session.chips -- the status line, and every picker a chip opens.

docs/plugin-api.md's second extension point, and the reason
``_refresh_status`` had to come apart: it was 157 lines of literal chip
construction, appending markup to one list and hint pairs to another and
relying on the two staying in step by hand.

It is now :meth:`PaneChipsMixin._status_chips`, which returns an ORDERED
sequence of :class:`StatusChip` records, and a four-line
``_refresh_status`` that renders them. Each record carries its own markup
AND its own tooltip rows, so the two can no longer drift; the sequence is
built in the order the chips paint, which is the ordering guarantee the
spec asks for ("a status line whose contents shift between sessions is
worse than one that omits something").

The tiers the chips fall into are unchanged and still documented at the
point each chip is built: SELECTOR chips open the shared ChipPicker,
ACTIONABLE chips run something, and plain chips inform. Plain has never
meant unexplained -- every chip here, including the plain ones, carries a
hint.

Nothing in this module loads anything. A plugin-contributed chip would be
another :class:`StatusChip` folded into the same sequence at its declared
order; that folding step is deliberately NOT built here.
"""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable  # noqa: F401 -- annotation-only

from textual.widgets import TabbedContent

from .. import config as config_mod
from .. import identity as identity_mod
from .. import peers as peers_mod
from ..engine import BELIEF_LIST_LIMIT, PENDING_LIST_LIMIT
from ..history import SessionSearch
from ..ui.beliefs import BeliefsBrowserTab
from ..ui.dialogs import ChipPicker, CompactConfirm, NeedsInputPopup, SlashComplete
from ..ui.labels import (
    memory_fill,
    memory_fill_chip,
    CLICKABLE_CHIP_ACCENT,
    CTX_ABSOLUTE_MIN_COLS,
    MODE_CHIP_MIN_COLS,
    MODE_EXPLAIN,
    _belief_scope_label,
    _chip_span,
    _fmt_belief_row,
    _fmt_pending_row,
    _one_line,
    as_proposal,
    belief_outcome_kind,
    belief_sort_key,
    ctx_chip,
    ellipsize,
    mode_chip,
    mode_text,
    mode_tooltip,
    proposal_tooltip,
    OUTCOME_EVENTS,
)
from ..ui.labels import ctx_text as ctx_text_of
from ..ui.statusline import StatusBar


# Item V: the one row in each of the two LORE pickers that LEAVES the
# picker. The chip stays a glance and the browser is the session -- both,
# rather than one replacing the other, because "roughly what does LORE
# believe about me" and "which of these 619 beliefs is stale and should
# these 166 proposals be applied" are not the same question and a dropdown
# only answers the first.
#
# The labels say what the browser HAS, never that it approves anything.
# The picker itself has no write affordance and gains none by pointing at
# one -- a row reading "approve" in a dropdown is the accidental-click
# surface item V exists to avoid.
BROWSE_ALL_ROW = (
    "browse:all",
    "▸ open the beliefs browser — evidence trails, timestamps, provenance",
)
REVIEW_ALL_ROW = (
    "review:all",
    "▸ open the beliefs browser — review these one at a time",
)


def _ctx_tooltip_absolute(used: "int | None", limit: "int | None") -> str:
    """The ctx chip's tooltip lead -- item X's actual guarantee.

    The inline `24k/200k` segment is optional and width-gated; THIS is
    not. Whatever the chip is painting, hovering it says how many tokens
    are in the window and how many the window holds, in full, with
    separators -- "12% of a 200k window" and "12% of a 1M window" are
    different situations and the percentage alone cannot tell them apart.

    An unreported limit is named as unreported. There is no fallback
    constant to reach for: DOXA drives several models, and the Models API
    was measured unreachable under OAuth-only auth in this project, so a
    hardcoded 200000 here would be a number DOXA made up presented in the
    same sentence as two it measured."""
    if used is None and limit is None:
        return "context window usage (the CLI has not reported any yet)"
    if limit is None:
        return (
            f"{used:,} tokens in the context window; its limit is not "
            "something this session's CLI reported"
        )
    if used is None:
        return f"context window holds {limit:,} tokens"
    return (
        f"{used:,} of {limit:,} tokens used, {max(limit - used, 0):,} left "
        "in the context window"
    )


@dataclass(frozen=True)
class StatusChip:
    """One chip on the status line: what it paints, and what it explains.

    ``key`` is the chip's own text, and is what gets painted when there is
    no ``markup``. The three constructors below are how a chip is
    ordinarily built; the field is public because a chip is a record, not
    an object with a private shape.

    ``markup`` is what actually paints when the chip is more than plain
    text: a clickable span, or (the git and ctx chips) markup the chip
    already built for itself. It is kept separate from ``key`` because
    wrapping already-trusted, code-generated coloring through the escaping
    click-span helper would escape ITS brackets as if they were arbitrary
    text.

    ``hints`` is the chip's tooltip rows -- a sequence, not one string,
    because one painted chip can carry several: the git chip is a single
    chip with a hint per span inside it. Carrying them on the same record
    as the markup is the whole point of this class. They used to be two
    parallel lists appended to twelve conditionals apart, and the only
    thing keeping them aligned was that nobody had edited one without the
    other yet."""

    key: str
    hints: "tuple[tuple[str, str], ...]" = field(default_factory=tuple)
    markup: "str | None" = None

    @classmethod
    def plain(cls, text: str, hint: str) -> "StatusChip":
        """An informational chip: no affordance, still explained."""
        return cls(key=text, hints=((text, hint),))

    @classmethod
    def clickable(cls, text: str, action: str, hint: str) -> "StatusChip":
        """A SELECTOR or ACTIONABLE chip: accent-colored, click runs
        ``action`` on the StatusBar (see :func:`doxa.ui.labels._chip_span`)."""
        return cls(key=text, hints=((text, hint),), markup=_chip_span(text, action))

    @classmethod
    def raw(
        cls, key: str, markup: str, hints: "tuple[tuple[str, str], ...]",
    ) -> "StatusChip":
        """A chip that already owns its own markup and its own hint rows."""
        return cls(key=key, hints=hints, markup=markup)

    def render(self) -> str:
        return self.key if self.markup is None else self.markup


class PaneChipsMixin:
    """SessionPane's status-line half. Mixed into the pane, never used
    standalone: every method here reads pane state through ``self``."""

    def _refresh_status(self) -> None:
        """Repaint the status line from :meth:`_status_chips`.

        Four lines, and deliberately so: the ONE place a chip's markup and
        its tooltip are joined is the chip record itself, so the two lists
        this method used to maintain in parallel -- and keep in step by
        hand across twelve conditional appends -- cannot drift apart any
        more."""
        if self.engine is None:
            return
        self.refresh_tab_label()
        chips = self._status_chips()
        bar = self.query_one("#status-bar", StatusBar)
        bar.update("  ·  ".join(chip.render() for chip in chips))
        bar.set_chip_hints([hint for chip in chips for hint in chip.hints])

    def _ctx_absolute_inline(self) -> bool:
        """Should the ctx chip print `24k/200k` beside its percentage?

        Two conditions, and both are about width, which is the scarcest
        thing on this row (:data:`~doxa.ui.labels.TAB_MODEL_MIN` and its
        neighbour exist one widget over for the same reason):

        1. the user asked for it -- ``DOXA_CTX_ABSOLUTE`` / the settings
           modal's "ctx: absolute tokens", off by default, because the
           tooltip already answers the question for free; and
        2. the terminal is at least
           :data:`~doxa.ui.labels.CTX_ABSOLUTE_MIN_COLS` wide.

        The second is the graceful degradation the status bar needs: a
        chip list that overflows does not scroll, it pushes the chips to
        its right off the end of the row, so the segment that is a
        convenience gives way to the chips that are information. An app
        that cannot be measured (no screen yet, at construction) counts as
        wide enough -- refusing to render something because the size is
        not known yet would make the chip flicker on at first repaint.

        Re-evaluated on the ordinary event-driven refreshes (boot, turn
        done, peer events), never from a resize hook: this whole bar is
        built under a documented no-timer, no-per-frame rule (see
        :class:`~doxa.ui.statusline.GitLine`), and ``_refresh_status``
        runs a belief COUNT(*) -- hanging that off every frame of a
        mouse-drag resize is exactly the idle-CPU regression this app
        already paid to shed. Narrowing a window therefore drops the
        segment at the next refresh, not mid-drag."""
        if not config_mod.raw("DOXA_CTX_ABSOLUTE").strip():
            return False
        width = getattr(getattr(self, "app", None), "size", None)
        width = getattr(width, "width", 0)
        return not width or width >= CTX_ABSOLUTE_MIN_COLS

    def _lore_slug(self) -> str:
        """This session's LORE project slug -- the one lore_core uses to
        pick MEMORY.md.

        Resolved through the MAIN repo root, not the pane's raw cwd, and
        that distinction is the whole point: since v0.17.0 every repo
        session runs in a worktree, so `project_slug(cwd)` answers "which
        DIRECTORY" when the question is "which PROJECT". A worktree at
        /tmp/claude-1000/doxa-mode resolves to the slug
        `-tmp-claude-1000-doxa-mode`, which owns no MEMORY.md -- so the
        project half of the memory chip silently vanished for every
        worktree session, i.e. the normal case (reported, v0.48.0).

        `peers.main_repo_root_of` maps a worktree back to its main
        checkout via `git rev-parse --git-common-dir`, and exists because
        this exact scope-key fracture bit the peer registry first. Reusing
        it keeps DOXA's answer to "which project am I in" in one place.
        Falls back to the raw cwd when there is no repo at all, which is
        what a repo-less session should get."""
        try:
            from lore_core.config import project_slug

            root = peers_mod.main_repo_root_of(self.cwd) or self.cwd
            return project_slug(root)
        except Exception:
            return ""

    def _mode_chip_cramped(self) -> bool:
        """Is the status row too narrow to spend full width on the mode?

        The other half of the width discipline :meth:`_ctx_absolute_inline`
        above starts, and it decides two things at once (see
        :meth:`_status_chips`): below this threshold the chip prints its
        SHORT form, and a chip that is merely reporting the safe default
        stands down entirely.

        The threshold is not a guess. An 80-column terminal was measured
        already full: adding an unconditional chip pushed the session
        handle off the right-hand end of the row, and the status bar has
        no overflow behaviour -- a chip that does not fit is not truncated
        or scrolled, it is simply gone. So an always-on mode chip does not
        cost width in the abstract; it costs whichever chip is furthest
        right, silently, on the terminal size most people actually use.

        What does NOT stand down at any width is a mode that stopped
        asking. That is the whole asymmetry: ``mode:default`` competing
        with the reattach handle for the last eight columns should lose,
        because it is telling the user what they already assume;
        ``⚠ mode:bypass`` should win against anything on the row, because
        it is the only place that fact appears.

        Same no-resize-hook rule as its neighbour: re-evaluated on the
        ordinary event-driven refreshes, so narrowing a window changes the
        chip at the next repaint rather than mid-drag. An app that cannot
        be measured yet (no screen, at construction) counts as wide."""
        width = getattr(getattr(self, "app", None), "size", None)
        width = getattr(width, "width", 0)
        return bool(width) and width < MODE_CHIP_MIN_COLS

    def _status_chips(self) -> "list[StatusChip]":
        """Every chip this pane's status line shows, in paint order.

        Tiers of chip, per the operator's own "for every chip?" question
        (v0.22.0 release notes), widened in v0.24.0: SELECTOR chips
        (model, branch, effort, and -- item 4, overriding v0.22.0's
        "repo name is INERT" -- the repo name) open the shared
        ChipPicker; ACTIONABLE chips (peers, ctx%, the session handle,
        beliefs) run something that already exists, or open a picker of
        their own; everything else (cost, sha, usage headroom) stays
        plain -- plain never meant unexplained, see StatusBar's own
        tooltip docstring (item 5): every chip below, including the
        plain ones, carries its hint.

        Hide-at-zero is the convention throughout: a chip whose number is
        zero, or whose state was never asserted, is omitted rather than
        shown empty. That is why this returns a list built by appends
        rather than a fixed-length one with holes in it.

        docs/plugin-api.md's second extension point is this sequence. A
        contributed chip would be one more :class:`StatusChip` placed at
        its declared order; DOXA still owns the rendering, which is what
        makes the ordering guarantee enforceable. No such folding step
        exists yet -- this release ships the shape, not the loader."""
        from .. import engine as engine_mod

        engine = self.engine
        chips: "list[StatusChip]" = []
        # Permission mode. FIRST on the row since v0.50.0, ahead of even
        # the model, and the reason is structural rather than editorial:
        # the status bar has no overflow behaviour -- a chip that does not
        # fit is not truncated or scrolled, it is simply gone -- so
        # position IS the guarantee. Anything after the first chip can be
        # pushed off by a long model id and a long branch name on a narrow
        # terminal. This is the only chip on the row that reports whether
        # the session will still stop and ask before it acts, and since
        # v0.50.0 a single keystroke can turn that off, so it is the one
        # chip that must never be the thing that falls off the end.
        #
        # It also no longer hides at ``default`` on a wide row: a
        # permission mode is ALWAYS in force, and ``default`` is a mode
        # with behavior rather than the absence of one. The single
        # exception is a CRAMPED row showing ``default`` -- there the chip
        # stands down, because at that width it would be spending columns
        # to tell the user what they already assume. Anything else is
        # painted at every width, short-form if it must be.
        #
        # ``mode_chip`` colors it with the values read out of the installed
        # Claude Code CLI (doxa.ui.labels documents the extraction), and
        # the click span is built HERE rather than through ``_chip_span``,
        # the same exception the ctx chip below takes and for the same
        # reason: that helper escapes its text, which would escape this
        # chip's own already-trusted, code-generated coloring as if it were
        # arbitrary bracket text. Unlike the ctx chip there is no accent
        # wrapper -- every mode carries a color of its own now, so letting
        # the clickable accent show through would mean painting a color
        # this chip did not measure.
        #
        # The KEY below is the PLAIN text, never the colored markup:
        # StatusBar._tooltip_for_x looks each chip up inside the bar's
        # markup-STRIPPED string, so a key still carrying `[#FF6B80]…[/]`
        # matches nothing and the tooltip silently vanishes at exactly the
        # tier that matters most. That is v0.35.0's ctx defect verbatim,
        # and this chip is unusually well placed to repeat it, so the rule
        # is written down here as well as there. The GLYPH is part of the
        # key, deliberately: it is text, it survives stripping, and the
        # lookup has to match what the widget actually paints.
        mode = str(getattr(engine, "permission_mode", None) or
                   engine_mod.DEFAULT_PERMISSION_MODE)
        cramped = self._mode_chip_cramped()
        if mode != engine_mod.DEFAULT_PERMISSION_MODE or not cramped:
            mode_plain = mode_text(mode, short=cramped)
            chips.append(StatusChip.raw(
                mode_plain,
                f"[@click=open_mode_picker]{mode_chip(mode, short=cramped)}[/]",
                ((mode_plain, mode_tooltip(mode)),),
            ))
        model = engine.model or "default"
        chips.append(StatusChip.clickable(
            model,
            "open_model_picker",
            "model handling this session's turns -- click to switch "
            "(takes effect on the NEXT turn, transcript kept)",
        ))
        if self.needs_input:  # visible only while a question or permission
            # request is actually pending on THIS pane.
            chips.append(StatusChip.plain(
                "⚑ needs input",
                "a question or permission request is waiting on this session",
            ))
        effort = getattr(engine, "effort", None)
        if effort:  # omitted when the CLI default is in force (no level
            # asserted at connect). A SELECTOR too, but its picker can only
            # ever affect a FUTURE session (connect-time only, same as
            # /effort) -- the picker itself says so rather than silently
            # no-opping.
            chips.append(StatusChip.clickable(
                f"effort:{effort}",
                "open_effort_picker",
                "reasoning effort for NEW sessions only (connect-time) -- "
                "click to change the default; this session keeps its own",
            ))
        git_chip = self._git.render(clickable=True) if self._git is not None else None
        if git_chip:  # hidden entirely outside a repo. ONE painted chip
            # with several hint rows inside it -- repo, branch and sha are
            # separate spans of the same string, and GitLine is the thing
            # that knows how they divide up.
            chips.append(StatusChip.raw(
                git_chip, git_chip, tuple(self._git.chip_hints()),
            ))
        # Subscription-aware cost: on subscription auth the session costs
        # no dollars, so a bare $ figure is misleading -- show the tier,
        # with the (already-computed) list-price figure demoted to an
        # explicit what-if. API-key auth keeps the real $ estimate.
        account = getattr(engine, "account", None) or {}
        tier = identity_mod.account_tier(account)
        if tier:
            # The "if API" words were dropped from the CHIP (they cost row
            # width, which is the scarcest thing in the status bar) but NOT
            # the meaning: `sub:` already says this session bills no
            # dollars, and `≈` marks the figure as an estimate. The full
            # statement stays one hover away, where there is room for it --
            # the same split /usage keeps, which spells it out in prose.
            chips.append(StatusChip.plain(
                f"sub:{tier} (≈${engine.total_cost_usd:.4f})",
                f"subscription plan ({tier}) -- no API dollars are actually "
                "spent; the ≈$ figure is the list-price what-if, i.e. what "
                "this session WOULD have cost on API pricing",
            ))
        else:
            chips.append(StatusChip.plain(
                f"${engine.total_cost_usd:.4f}",
                "actual API spend billed for this session so far",
            ))
        if self._usage_chip:  # only when real numbers exist
            chips.append(StatusChip.plain(
                self._usage_chip,
                "subscription utilization cached by the claude CLI -- "
                "session (5h) and weekly limits used so far",
            ))
        # ctx% is ACTIONABLE (click -> confirm, then /compact -- item 1)
        # but its own markup is already trusted, code-generated pressure
        # coloring (ctx_chip's amber/red escalation) -- wrapping it through
        # _chip_span would escape THOSE brackets as if they were arbitrary
        # text, same defect a literal `[` in a model-chosen label would
        # risk the other way. So the click span is built directly here, no
        # _escape_markup: the accent shows through at the "normal" tier
        # (no inner color) and yields to the pressure color once one
        # applies -- the pressure signal outranks the click affordance.
        #
        # Item X (ctx absolute): the percentage and the absolute token
        # counts are ONE reading (SessionEngine._safe_ctx_usage), so they
        # are one chip. The inline `24k/200k` half is opt-in and
        # width-gated -- see _ctx_absolute_inline -- but the numbers
        # themselves are UNCONDITIONAL in the tooltip below, which is what
        # actually answers "12% of what?" without spending a column.
        used = getattr(engine, "last_ctx_tokens", None)
        limit = getattr(engine, "last_ctx_max_tokens", None)
        inline = self._ctx_absolute_inline()
        ctx_plain = ctx_text_of(
            engine.last_ctx_percentage, used, limit, absolute=inline
        )
        ctx_markup = ctx_chip(
            engine.last_ctx_percentage, used, limit, absolute=inline
        )
        chips.append(StatusChip.raw(
            # The KEY (and the tooltip's match string) is the PLAIN text,
            # never the colored markup: StatusBar._tooltip_for_x looks the
            # chip up inside the bar's markup-stripped string, so a key
            # still carrying `[#D9534F]…[/]` matched nothing and the ctx
            # tooltip silently vanished at the amber and red tiers.
            ctx_plain,
            f"[@click=compact_now][{CLICKABLE_CHIP_ACCENT}]{ctx_markup}[/][/]",
            ((
                ctx_plain,
                f"{_ctx_tooltip_absolute(used, limit)} -- click to "
                "compact (asks first: compacting summarizes and discards "
                "earlier detail)",
            ),),
        ))
        chips.append(StatusChip.clickable(
            f"{engine.belief_count()} beliefs",
            "open_beliefs_picker",
            "active beliefs LORE holds for this session -- click for the "
            "grouped list, or /beliefs for the full browser (evidence, "
            "age, provenance, staged proposals)",
        ))
        # Curated-memory fill, right after the belief count: both answer
        # "what does LORE hold for this session", and the caps are the one
        # LORE number that fails a WRITE when exceeded rather than
        # degrading quietly -- a user at 88% wants to see it before the
        # refusal, not after. Two percentages, not one: the caps are
        # separate and fill at different rates.
        mem = memory_fill_chip(
            memory_fill("user"),
            memory_fill("project", self._lore_slug()),
        )
        if mem is not None:
            chips.append(StatusChip.plain(*mem))
        subagent_count = len(self._subagents)
        if subagent_count:  # hidden at 0 -- same convention as peers below
            noun = "agent" if subagent_count == 1 else "agents"
            chips.append(StatusChip.plain(
                f"⧉ {subagent_count} {noun}",
                "subagent tasks running right now -- see the row below "
                "to open one's own transcript",
            ))
        if getattr(engine, "detachable", False):
            sid = str(getattr(engine, "session_id", "") or "")
            if sid:  # attached to a daemon: show the reattach handle --
                # ACTIONABLE (item 2 -- opens a sessions picker; a plain
                # copy-to-clipboard is still one row inside it). The accent
                # color replaces the old #8A8073 dim treatment, since a
                # clickable chip wears the SAME affordance every other one
                # does rather than staying visually "quiet".
                chips.append(StatusChip.clickable(
                    f"⌁ session {sid[:8]}",
                    "open_sessions_picker",
                    "this session's reattach handle -- click to see every "
                    "session in scope, including detached ones",
                ))
        peer_count = engine.peer_count()
        if peer_count:  # hidden at 0 -- a solo session has no peers chip
            # Under detach-by-default a bare count is ambiguous: four live
            # peers could be four colleagues or two sessions the user left
            # running an hour ago. The ⌁ suffix (the same glyph the attach
            # handle wears) says how many are running with nobody watching
            # -- and is omitted entirely at zero, because "(0⌁)" is noise
            # on the common case. /sessions is where the number leads.
            detached = sum(1 for peer in engine.list_peers() if peer.clients == 0)
            chips.append(StatusChip.clickable(
                f"peers {peer_count}" + (f" ({detached}⌁)" if detached else ""),
                "open_sessions",
                "other DOXA sessions working on this repo -- click for the "
                "full list; (N⌁) counts how many are detached",
            ))
        disabled = engine.disabled_tools()
        if disabled:  # two-strikes containment note -- hidden when empty
            chips.append(StatusChip.plain(
                " ".join(f"⊘ {name}" for name in disabled),
                "tool disabled after repeated failures this session "
                "(two-strikes containment)",
            ))
        return chips

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
        groups: "dict[str, str] | None" = None,
        collapsible: bool = False,
        group_notes: "dict[str, str] | None" = None,
        counted_noun: str = "",
        open_groups: "set[str] | None" = None,
    ) -> None:
        """Shared entry point for every chip that opens :class:`ChipPicker`
        -- guards against opening UNDER a pending needs-input request (same
        "the question owns this row" rule ``_on_prompt_changed`` already
        applies to the two prompt-driven popups) and closes those two
        popups first, so at most one of the four ever shows at once."""
        if self.query_one("#needs-input-popup", NeedsInputPopup).is_open:
            return
        self.query_one("#slash-complete", SlashComplete).close()
        self.query_one("#session-search", SessionSearch).close()
        self.query_one("#chip-picker", ChipPicker).open(
            rows, current_id, on_select, note=note, title=title, groups=groups,
            collapsible=collapsible, group_notes=group_notes,
            counted_noun=counted_noun, open_groups=open_groups,
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
        from .. import engine as engine_mod

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

    async def open_mode_picker(self) -> None:
        """The permission-mode chip's click target (v0.42.0).

        Lists ALL SIX modes, cycle-safe ones first, each row carrying the
        one-sentence explanation the chip's tooltip and ``/mode``'s own
        listing use (``labels.MODE_EXPLAIN`` -- one source, three
        surfaces). The gated three are listed rather than hidden: a
        capability the CLI has and DOXA refuses to mention is how a user
        ends up editing config files to reach it, and the confirmation
        (not the concealment) is what makes reaching it deliberate.

        Selection calls the SAME ``_cmd_mode`` coroutine ``/mode <name>``
        uses -- one switch path, three doors (keycap, chip, command), and
        exactly one place where the confirmation for a gated mode lives.
        Picking a gated row here therefore raises the confirm, and
        declining it leaves the session where it was."""
        from .. import engine as engine_mod

        if self.engine is None:
            return
        rows = [
            (name,
             ("⚠ " if name in engine_mod.UNASKED_MODES else "")
             + f"{name} — {MODE_EXPLAIN.get(name, '')}")
            for name in engine_mod.PERMISSION_MODES
        ]
        # Grouped by the axis a user is actually choosing on: can I get
        # here with the key, or do I have to mean it? Since v0.50.0 that
        # is no longer the same as "is this one safe" -- the top group
        # now contains two modes where nothing will ask -- so the row
        # labels say what each group IS rather than implying safety.
        groups = {
            name: ("Shift+Tab reaches these" if name in engine_mod.CYCLE_MODES
                   else "/mode only — confirms first")
            for name in engine_mod.PERMISSION_MODES
        }
        current = str(getattr(self.engine, "permission_mode", None) or "default")
        self._open_chip_picker(
            rows, current,
            lambda chosen: self.run_worker(self._cmd_mode(chosen), group="command"),
            note="⚠ = DOXA will not ask you about a tool call in that mode",
            title="permission mode", groups=groups,
        )

    def run_status_command(self, name: str) -> None:
        """The peers chip's click target -- runs a slash command exactly
        as if it had been typed and submitted (``_run_command`` is the
        SAME dispatch ``on_prompt_submitted`` uses for a non-passthrough
        row)."""
        self.run_worker(self._run_command(name), group="command")

    def run_compact_now(self) -> None:
        """The ctx% chip's click target (item 1) -- v0.22.0 sent
        ``/compact`` on the FIRST click, no warning. That was a real
        defect, not a preference: compaction is lossy (the transcript
        itself gets summarized away; the PreCompact review that runs first
        does not change that) and there is no undo, so one misclick
        silently discarded conversation detail. This now asks first
        (:class:`CompactConfirm`) and only fires the turn on acceptance --
        ``/compact`` is still a PASSTHROUGH row (doxa/commands.py: the
        literal prompt text is what triggers compaction and fires the
        PreCompact hook), so accepted dispatch is still a turn, not a
        command, the same ``run_worker(self._run_turn(...))`` call
        ``on_prompt_submitted`` would make for that same text."""
        if self.engine is None:
            return
        self.run_worker(self._confirm_and_compact(), group="compact-confirm")

    async def _confirm_and_compact(self) -> None:
        engine = self.engine
        if engine is None:
            return
        accepted = await self.app.push_screen_wait(
            CompactConfirm(engine.last_ctx_percentage)
        )
        if not accepted:
            return  # Esc / decline: no compaction, no turn sent, status
            # bar unchanged -- exactly item 1's own contract.
        self.run_worker(self._run_turn("/compact"), exclusive=True, group="turn")

    def copy_session_handle(self) -> None:
        """The clipboard capability the session-handle chip's click used to
        BE, on its own, through v0.22.0 -- kept as the first row of the
        sessions picker it opens now (item 2), rather than silently
        dropped: a modifier-click would be less discoverable than a real
        row in the same dropdown every other selection already uses."""
        sid = str(getattr(self.engine, "session_id", "") or "")
        if not sid:
            return
        self.app.copy_to_clipboard(sid)
        self.run_worker(self._system(f"copied session handle: {sid[:8]}…"), group="command")

    def open_sessions_picker(self) -> None:
        """The session-handle chip's click target (item 2) -- v0.22.0 just
        copied the handle. The operator wants a dropdown of every session
        IN SCOPE instead, detached ones clearly marked, the current one
        marked too -- only ever reachable when the chip itself is showing
        (hide-at-zero: an attached, detachable session), so
        ``engine.session_id`` is never empty here in practice.

        Scope key: the SAME ``main_repo_root_of(cwd) or cwd`` PeerHost
        itself computes for presence (doxa.peers) -- read here rather than
        re-derived differently, via the SAME "engine cwd wins over the
        pane's own" fallback ``_boot``/``_cmd_search``/``_identity_text``
        already use (attach can land a pane in another project).

        Detached marker: ``list_daemons`` returns ``PeerInfo`` rows whose
        ``clients`` field is exactly what the peers chip already reduces to
        its own ``(N⌁)`` suffix (0 clients == detached, see
        ``PeerInfo.clients``'s own docstring) -- reused here, not
        re-derived, and rendered with the SAME ⌁ glyph."""
        engine = self.engine
        if engine is None:
            return
        cwd = str(getattr(engine, "cwd", None) or self.cwd)
        scope = peers_mod.main_repo_root_of(cwd) or cwd
        daemons = peers_mod.list_daemons(scope_key=scope)
        current_sid = str(getattr(engine, "session_id", "") or "")
        open_by_sid = {
            str(getattr(p.engine, "session_id", "") or ""): p
            for p in self.app.panes()
        }
        rows: "list[tuple[str, str]]" = [("__copy__", "⧉ copy this session's handle")]
        for entry in daemons:
            marker = "  ⌁ detached" if entry.clients == 0 else ""
            rows.append((
                entry.session_id, f"{entry.title}  {entry.session_id[:8]}{marker}",
            ))
        self._open_chip_picker(
            rows, current_sid,
            lambda rid: self._select_session_row(rid, daemons, open_by_sid),
            title="sessions",
        )

    def _select_session_row(
        self,
        rid: str,
        daemons: "list[peers_mod.PeerInfo]",
        open_by_sid: "dict[str, Any]",
    ) -> None:
        """Item 2's own spec, verbatim: the current session's row is a
        no-op; a detached (or otherwise not-open-HERE) daemon is attached
        to via the SAME path `doxa attach` and the palette's own
        "Attach: ..." entries use (``DoxaApp._cmd_attach`` -- never a
        second attach implementation). One judgment call not in that spec:
        a session already open in ANOTHER tab of this window switches to
        that tab (``DoxaApp._switch_to_tab``, the same path Ctrl+Left/
        Right and the palette's tab entries use) instead of attaching a
        SECOND client to it from here -- the palette's own Attach section
        makes the identical exclusion for the identical reason."""
        if rid == "__copy__":
            self.copy_session_handle()
            return
        current_sid = str(getattr(self.engine, "session_id", "") or "")
        if rid == current_sid:
            return
        other_pane = open_by_sid.get(rid)
        if other_pane is not None and other_pane is not self:
            self.app._switch_to_tab(getattr(other_pane, "id", None) or "")
            return
        entry = next((e for e in daemons if e.session_id == rid), None)
        if entry is None:
            return
        self.app._cmd_attach(entry)

    async def open_beliefs_picker(self) -> None:
        """The beliefs chip's click target (item 3) -- v0.22.0 left it
        plain ("no `/beliefs`-ish surface exists to route to", per its own
        release notes). This is a LIGHTWEIGHT viewer, not lettered item V
        (the full beliefs browser -- evidence trails, approve/reject); a
        row's selection surfaces its full claim + confidence inline (a
        SystemBlock) -- the least-surprising small thing a claim-summary
        row can do, and a deliberately narrow judgment call (see
        CHANGELOG: item V still owns the real browser).

        Cost discipline: ``belief_count()`` (the chip's own number) runs on
        EVERY status refresh and must stay free of the belief BODIES --
        this calls ``list_beliefs()`` instead, the heavier claim-text
        query, and only ever from a click, never from `_refresh_status`
        (asserted by tests/test_status_chips.py).

        v0.28.0 -- reported: "clicking on 'beliefs' chip leads to error
        message 'too much for a message'", with the follow-up "it was
        supposed to be shown in an autocomplete dropdown". Two things had
        to change and NEITHER of them is here: the daemon now serves this
        list in frame-sized pages and EngineClient reassembles them (a
        detached session's single reply carrying ~517 claim bodies was
        being discarded by doxa.daemon.encode_frame and replaced with
        "reply exceeded the frame cap", which the except-arm below then
        printed as a system message -- the "error" the operator saw where
        the dropdown should have been). What IS here is the honesty half:
        a list that ends because it hit :data:`engine.BELIEF_LIST_LIMIT`
        rather than because the store ran out now SAYS so, in the picker's
        own note row, checked against the same ``belief_count()`` COUNT(*)
        the chip itself displays. A short list must never be shown as if
        it were the whole store."""
        engine = self.engine
        if engine is None:
            return
        lister = getattr(engine, "list_beliefs", None)
        if lister is None:
            await self._system("beliefs: this session's handle cannot list beliefs")
            return
        try:
            beliefs = await lister()
        except Exception as exc:  # noqa: BLE001 -- a refusal is information
            await self._system(f"beliefs: {type(exc).__name__}: {exc}")
            return
        if not beliefs:
            await self._system("beliefs: none active")
            return
        # Asked once, here, on a path that is already awaiting the store --
        # never on a status refresh, and never from inside the action menu
        # where a socket round trip would stall a keystroke.
        if self._belief_action_state_cached() is None:
            await self._prime_belief_action_state()
        # Sorted by GROUP first (stable -- so ChipPicker's group-header
        # insertion, which walks rows in the order given, still produces
        # clean contiguous blocks rather than a header reappearing every
        # time two subjects interleave) and then, INSIDE each group, by
        # what reality has said (v0.48.0). belief_sort_key already existed
        # for the browser and the picker simply never used it: tested
        # beliefs to the top of their group, most recently tested first,
        # never-tested following as a stable bucket. With ~2% of a real
        # store carrying any verdict at all, a picker that buried those is
        # a picker that hid the only evidence it holds.
        ordered = sorted(
            beliefs,
            key=lambda b: (_belief_scope_label(str(b.get("subject") or "")),
                           belief_sort_key(b)),
        )
        rows: "list[tuple[str, str]]" = [BROWSE_ALL_ROW]
        # Its own group header, because ChipPicker inserts one whenever the
        # group changes and an EMPTY label would paint a bare "▎ " row.
        groups: "dict[str, str]" = {BROWSE_ALL_ROW[0]: "browse"}
        by_id: "dict[str, dict]" = {}
        tested: "dict[str, int]" = {}
        for belief in ordered:
            rid = f"belief:{belief.get('id')}"
            rows.append((rid, _fmt_belief_row(belief)))
            group = _belief_scope_label(str(belief.get("subject") or ""))
            groups[rid] = group
            by_id[rid] = belief
            if belief_outcome_kind(belief) in OUTCOME_EVENTS:
                tested[group] = tested.get(group, 0) + 1
        # The header annotation ChipPicker cannot compute for itself: how
        # many of a group's beliefs reality has ever tested. It is the
        # number this store makes interesting -- 15 of 635 on the reporting
        # operator's -- and a folded group that says "412 beliefs, 3
        # tested" has answered a question the expanded list would have
        # taken six hundred rows to answer.
        group_notes = {group: f"{n} tested" for group, n in tested.items()}
        self._open_chip_picker(
            rows, None,
            lambda rid: self._pick_belief_row(rid, by_id),
            title="beliefs", groups=groups,
            note=self._belief_cap_note(engine, len(rows) - 1),
            collapsible=True, group_notes=group_notes, counted_noun="belief",
            # The browse row is a DOOR, not data: folding it would hide the
            # way into the browser behind exactly the fold a 635-belief
            # store makes necessary.
            open_groups={"browse"},
        )

    def _pick_belief_row(self, rid: str, by_id: "dict[str, dict]") -> None:
        """A picker row's destination: the browser, or THIS belief's own
        actions.

        v0.27.0 spilled the claim inline and stopped there; v0.48.0 makes
        that the first row of a per-belief menu instead, because the user
        asked for a button on every row and an ``OptionList`` cannot give
        one -- an ``Option`` has no widgets, no tooltip and exactly one
        click target. What it CAN do is reopen itself against a new row set,
        which is the pattern the repo picker has used since v0.22.0 to
        descend a directory. So the row's own actions become the row set.

        That shape is also the right one for safety rather than a
        consolation for the wrong one. A dropdown row is one Enter away
        from whatever the highlight is sitting on, which makes it a MORE
        accidental surface than the browser's full-height rows, not less --
        so nothing here acts on the belief you selected. It shows you what
        can be done to it, named for what it actually does."""
        if rid == BROWSE_ALL_ROW[0]:
            self.run_worker(self.open_beliefs_browser(), group="command")
            return
        belief = by_id.get(rid)
        if belief is not None:
            # Deferred one refresh cycle, for the reason
            # :meth:`_select_repo_row` already documents at length:
            # ChipPicker.select_row has ALREADY called close() and is about
            # to hand focus back to the prompt, so reopening the same
            # instance synchronously races Textual's queued Blur delivery
            # and gets closed right back. Same fix, same call.
            self.app.call_after_refresh(
                partial(self._open_belief_actions, belief, by_id)
            )

    def _open_belief_actions(
        self, belief: dict, by_id: "dict[str, dict]", *, arm_retract: bool = False,
    ) -> None:
        """One belief's actions, as a picker.

        WHY THESE VERBS AND NOT "APPROVE"/"REJECT". The user asked for
        approve/reject on every belief row, and those two are not
        operations on a belief. Approving applies a STAGED PROPOSAL -- an
        entry that does not exist yet -- and every proposal already carries
        approve and reject in the browser. A belief is a claim that is
        already in the store and already steering the model; the things
        LORE can actually do to one are record what reality did to it
        (``belief_outcomes``: confirmed / contradicted / stale) and end it
        (retract). So those are the verbs, spelled LORE's way.

        Recording an outcome is the high-value one and the reason this menu
        is worth the keystroke: 97.6% of a live store has never been tested
        by anything, and every ``calibrated_confidence`` in the product is
        reading a curve built on that nothing.

        RETRACT ARMS. It is the destructive verb -- the belief leaves the
        working set and the model's context -- and this is a dropdown, so
        it takes a second, separately-worded selection on the same menu.
        Same misclick asymmetry the browser's approve control carries, in
        the idiom this widget has."""
        bid = belief.get("id")
        claim = ellipsize(_one_line(str(belief.get("claim") or ""), 200), 46)
        rows: "list[tuple[str, str]]" = [
            ("act:show", "▸ show the full claim"),
            ("act:confirmed", "✓ confirmed — reality agreed with this"),
            ("act:contradicted", "✗ contradicted — reality disagreed with this"),
            ("act:stale", "⌛ stale — no longer applies"),
        ]
        if arm_retract:
            rows.append(
                ("act:retract!", "⌫ RETRACT — select again to end this belief")
            )
        else:
            rows.append(("act:retract", "⌫ retract this belief…"))
        rows.append(("act:back", "← back to the belief list"))
        state = self._belief_action_state_cached()
        note = f"belief {bid} · {claim}"
        if state is not None and not state.get("capable"):
            note = f"{note}\nread-only — {state.get('reason') or 'actions unavailable'}"
            rows = [r for r in rows if r[0] in ("act:show", "act:back")]
        self._open_chip_picker(
            rows, None,
            lambda rid: self._run_belief_action(rid, belief, by_id),
            title=f"belief {bid}", note=note,
        )

    def _belief_action_state_cached(self) -> "dict | None":
        """The engine's belief-action capability, fetched once per pane.

        Cached because this menu opens on a keystroke and the daemon path
        is a socket round trip -- and because the answer cannot change
        without the process that holds lore_core restarting. None means
        "not asked yet", which renders as no caveat rather than as a
        guessed one; the write itself is gated engine-side regardless, so
        an unfetched cache can never turn into an ungated action."""
        return getattr(self, "_belief_actions_state", None)

    async def _prime_belief_action_state(self) -> None:
        engine = self.engine
        asker = getattr(engine, "belief_action_state", None) if engine else None
        if asker is None:
            return
        try:
            result = asker()
            if inspect.isawaitable(result):
                result = await result
            self._belief_actions_state = dict(result or {})
        except Exception:  # noqa: BLE001 -- a caveat is never worth raising
            self._belief_actions_state = None

    def _run_belief_action(
        self, rid: str, belief: dict, by_id: "dict[str, dict]",
    ) -> None:
        """Dispatch one selected action. Never acts on more than the belief
        whose menu this is -- there is no id list anywhere on this path."""
        if rid == "act:back":
            self.app.call_after_refresh(
                lambda: self.run_worker(self.open_beliefs_picker(),
                                        group="command")
            )
            return
        if rid == "act:show":
            self._show_belief_detail(belief)
            return
        if rid == "act:retract":
            # First selection ARMS. Reopening the same menu with the armed
            # row is the whole confirmation: it is worded differently, it
            # is the only row that changed, and it is not where the
            # highlight was. Deferred for the close/blur/focus reason
            # _select_repo_row documents.
            self.app.call_after_refresh(
                partial(self._open_belief_actions, belief, by_id,
                        arm_retract=True)
            )
            return
        if rid == "act:retract!":
            self.run_worker(self._retract_belief(belief), group="command")
            return
        if rid.startswith("act:"):
            self.run_worker(
                self._record_belief_outcome(belief, rid.split(":", 1)[1]),
                group="command",
            )

    async def _record_belief_outcome(self, belief: dict, event: str) -> None:
        engine = self.engine
        recorder = getattr(engine, "record_belief_outcome", None) if engine else None
        bid = belief.get("id")
        if recorder is None:
            await self._system(
                "beliefs: this session's handle cannot record an outcome"
            )
            return
        try:
            error = await recorder(bid, event)
        except Exception as exc:  # noqa: BLE001 -- a refusal is information
            error = f"{type(exc).__name__}: {exc}"
        claim = _one_line(str(belief.get("claim") or ""), 160)
        if error:
            # Not silently a failure and not silently a success: LORE's own
            # dormancy note comes back through this same channel, so a
            # contradiction that just retired a belief says so here.
            await self._system(f"belief {bid} · {event} · {error}\n\n{claim}")
            return
        await self._system(
            f"belief {bid} recorded as {event} (source: user)\n\n{claim}"
        )

    async def _retract_belief(self, belief: dict) -> None:
        engine = self.engine
        retractor = getattr(engine, "retract_belief", None) if engine else None
        bid = belief.get("id")
        if retractor is None:
            await self._system("beliefs: this session's handle cannot retract")
            return
        try:
            error = await retractor(bid)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        claim = _one_line(str(belief.get("claim") or ""), 160)
        if error:
            await self._system(f"belief {bid} · NOT retracted — {error}\n\n{claim}")
            return
        await self._system(
            f"belief {bid} retracted — it leaves the working set and the "
            f"model's context. Its evidence and outcome ledger stay on "
            f"disk.\n\n{claim}"
        )

    async def open_beliefs_browser(self) -> None:
        """Item V: open (or bring forward) this pane's beliefs browser.

        A TabPane in the shared ``#session-tabs`` strip, mounted the same
        way :meth:`PaneRuntimeMixin.open_transcript` mounts a subagent's
        transcript tab -- one browser per pane, reopening activates the
        existing one rather than stacking a second, and focus moves into
        the new tab so the keyboard route to a row's own approve/reject
        works the moment it opens.

        The pane keeps its own reference (``_beliefs_tab``) rather than
        searching the strip by id: a tab the user closed must not be
        resurrected by a stale query, and a dropped reference is how this
        pane learns that happened (see the guard below, which re-checks
        the tab is still mounted before activating it)."""
        try:
            tabbed = self.app.query_one("#session-tabs", TabbedContent)
        except Exception:  # noqa: BLE001 -- app mid-teardown; nothing to open
            return
        existing = getattr(self, "_beliefs_tab", None)
        if existing is not None and existing.is_mounted:
            tabbed.active = existing.id or tabbed.active
            if existing.rows:
                existing.rows[0].focus()
            else:
                existing.scroll.focus()
            return
        tab = BeliefsBrowserTab(self, id=f"beliefs-{self.id}")
        self._beliefs_tab = tab
        await tabbed.add_pane(tab)
        tabbed.active = tab.id or tabbed.active
        tab.scroll.focus()

    @staticmethod
    def _belief_cap_note(engine: "Any", shown: int) -> str:
        """The picker's row-0 caveat when the list is SHORTER than the
        store, and "" when it is complete -- ``belief_count()`` is the same
        ``COUNT(*) WHERE status='active'`` predicate ``list_beliefs``
        selects over, on both engines, so the two numbers are directly
        comparable and a mismatch means exactly one thing: the cap bit."""
        counter = getattr(engine, "belief_count", None)
        try:
            total = int(counter() or 0) if callable(counter) else 0
        except Exception:  # noqa: BLE001 -- a caveat is never worth raising
            return ""
        if total <= shown:
            return ""
        return (
            f"showing {shown} of {total} active beliefs -- this list is "
            f"capped at {BELIEF_LIST_LIMIT}"
        )

    def _show_belief_detail(self, belief: "dict | None") -> None:
        if belief is None:
            return
        subject = str(belief.get("subject") or "")
        confidence = belief.get("confidence")
        conf_text = (
            f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "?"
        )
        claim = str(belief.get("claim") or "")
        if belief.get("claim_truncated"):
            # doxa.daemon._fit_belief_page cut this one to make it fit a
            # single wire frame at all (a claim bigger than the 64KB cap by
            # itself). Say so -- a silently shortened claim read as the
            # whole belief is exactly the dishonesty the paging avoids
            # everywhere else.
            claim += "\n\n[claim truncated -- larger than one wire frame]"
        self.run_worker(
            self._system(
                f"belief · {_belief_scope_label(subject)} ({subject}) · "
                f"confidence {conf_text}\n\n{claim}"
            ),
            group="command",
        )

    async def open_pending_picker(self) -> None:
        """``/pending`` -- the staged proposals the background reviewer put
        behind LORE's approval gate, listed in the SAME
        :class:`ChipPicker` the beliefs chip opens, with the full text of
        a selected row spilled into a system block. Reached from the
        prompt, from the Ctrl+P palette, and from the click target on the
        notification block itself.

        This exists because the notification it replaces pointed at
        ``/lore:pending``, which is a Claude Code PLUGIN command: it is
        not in ``doxa.commands.REGISTRY``, so typing it inside DOXA
        reaches the model rather than a list. Telling a user to run a
        command that does not exist where they are reading the message is
        a dead end, and the fix is a native surface, not a better
        sentence.

        STILL READ-ONLY, and since item V that is a division of labour
        rather than a scope boundary. Every row now says WHAT APPROVING IT
        WOULD DO (:func:`doxa.ui.labels._fmt_pending_row`), because a row
        that does not is not reviewable -- but a dropdown is a glance, and
        approving is not something to do at a glance. The approve and
        reject controls live on the rows of the beliefs browser, one per
        proposal, and this picker's first row is the door to it."""
        engine = self.engine
        if engine is None:
            return
        lister = getattr(engine, "list_pending", None)
        if lister is None:
            await self._system(
                "pending: this session's handle cannot list staged proposals"
            )
            return
        try:
            proposals = await lister()
        except Exception as exc:  # noqa: BLE001 -- a refusal is information
            await self._system(f"pending: {type(exc).__name__}: {exc}")
            return
        # Looking at the list is the strongest possible "I have seen it".
        self.set_staged(False)
        if not proposals:
            await self._system(
                "pending: nothing staged — the background reviewer has "
                "proposed nothing awaiting your approval"
            )
            return
        rows = [REVIEW_ALL_ROW]
        rows += [(f"pending:{index}", _fmt_pending_row(item))
                 for index, item in enumerate(proposals)]
        by_id = {f"pending:{index}": item for index, item in enumerate(proposals)}
        note = ""
        if len(proposals) >= PENDING_LIST_LIMIT:
            # Same honesty rule the beliefs picker's cap note follows: a
            # list that ended because it hit the cap must never be shown
            # as if the staging area had run out.
            note = (
                f"showing the first {len(proposals)} staged proposals -- this "
                f"list is capped at {PENDING_LIST_LIMIT}"
            )
        self._open_chip_picker(
            rows, None,
            lambda rid: self._pick_pending_row(rid, by_id),
            title="pending", note=note,
        )

    def _pick_pending_row(self, rid: str, by_id: "dict[str, Any]") -> None:
        """A pending row's destination: the browser, or one proposal
        inline. Same shape as :meth:`_pick_belief_row`."""
        if rid == REVIEW_ALL_ROW[0]:
            self.run_worker(self.open_beliefs_browser(), group="command")
            return
        self._show_pending_detail(by_id.get(rid))

    def _show_pending_detail(self, item: "dict | str | None") -> None:
        """One staged proposal, in full, as a system block -- the same
        "a row's selection surfaces its whole body inline" shape
        :meth:`_show_belief_detail` uses, and for the same reason: the
        picker row is ellipsized, and the least surprising thing a
        truncated row can do is show you the rest.

        Since item V it leads with the proposed VERDICT, because what
        approving would change is the thing a reader of this block is
        deciding about. Still read-only: the block names the browser as
        where the decision is made rather than making it here."""
        if not item:
            return
        proposal = as_proposal(item)
        self.run_worker(
            self._system(
                "staged proposal · awaiting your approval\n"
                f"{proposal_tooltip(proposal)}"
            ),
            group="command",
        )

    def open_repo_picker(self) -> None:
        """The repo-name chip's click target (item 4, overriding v0.22.0's
        "repo name is INERT"): a directory-walking picker, starting at
        this session's own cwd -- typing filters the CURRENT listing
        (ChipPicker's own type-to-filter, unchanged), selecting a plain
        directory DESCENDS into it (the picker re-opens itself at the new
        listing -- see ChipPicker.open's own "reopening" docstring: a
        second ``open()`` call mid-display just reconfigures the same
        instance), and selecting a directory that IS a git repo root opens
        it in a NEW TAB."""
        cwd = str(getattr(self.engine, "cwd", None) or self.cwd)
        self._open_repo_picker_at(cwd)

    def _repo_picker_rows(
        self, directory: str
    ) -> "tuple[list[tuple[str, str]], str | None]":
        """Directory entries of ``directory`` as ChipPicker rows, each rid
        prefixed ``dir:`` (descend) or ``repo:`` (a git repo root -- open
        in a new tab): repo-ness is ``peers.main_repo_root_of``, reused
        rather than re-derived (same function PeerHost's own scope key and
        the spawn-or-attach reuse path already call). Returns
        ``(rows, error)`` -- ``error`` set (rows still whatever could be
        built, usually just the parent row) on an unreadable/nonexistent
        directory, so a stale race between listing and clicking degrades
        to a message, never a crash."""
        rows: "list[tuple[str, str]]" = []
        parent = os.path.dirname(directory.rstrip(os.sep) or os.sep)
        if parent and parent != directory:
            rows.append((f"dir:{parent}", ".. (up)"))
        # "open here" is offered for ANY directory since v0.28.0, not only a
        # git repo root. Before, descending into a plain directory left the
        # picker with no row that DID anything -- every row either went
        # deeper or went up, which is a dead end the operator hit ("when i
        # chose a dir and click on one, it is not changed"). open_tab_at
        # takes any directory; only the ⎇ marker is about repo-ness.
        is_repo = peers_mod.main_repo_root_of(directory) == directory
        rows.append((
            f"repo:{directory}",
            f"· open here ({Path(directory).name})" + (" ⎇" if is_repo else ""),
        ))
        try:
            entries = sorted(
                (e for e in os.scandir(directory)
                 if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")),
                key=lambda e: e.name.lower(),
            )
        except OSError as exc:
            return rows, f"cannot list {directory}: {exc}"
        for entry in entries:
            path = entry.path
            if peers_mod.main_repo_root_of(path) == path:
                rows.append((f"repo:{path}", f"{entry.name} ⎇"))
            else:
                rows.append((f"dir:{path}", f"{entry.name}/"))
        return rows, None

    def _open_repo_picker_at(self, directory: str) -> None:
        if not os.path.isdir(directory):
            self.run_worker(
                self._system(f"repo: not a directory: {directory}"), group="command",
            )
            return
        rows, err = self._repo_picker_rows(directory)
        if err:
            self.run_worker(self._system(f"repo: {err}"), group="command")
            if not rows:
                return
        self._open_chip_picker(
            rows, None, self._select_repo_row, title=f"repo · {directory}",
        )

    def _select_repo_row(self, rid: str) -> None:
        kind, _, path = rid.partition(":")
        if kind == "dir":
            # Deferred one refresh cycle, deliberately: ChipPicker.
            # select_row has ALREADY called close() (display=False) and is
            # about to hand focus back to the prompt by the time this
            # callback runs -- reopening the SAME instance synchronously,
            # right here, races that hand-off (Textual's own Blur delivery
            # for the picker's just-lost focus is queued, not immediate,
            # and would land AFTER a synchronous reopen and close it right
            # back). call_after_refresh runs this once that whole
            # close/blur/focus cycle has actually settled.
            self.app.call_after_refresh(partial(self._open_repo_picker_at, path))
        elif kind == "repo":
            self._spawn_tab_at(path)

    def _spawn_tab_at(self, path: str) -> None:
        """A CHOSEN repo opens in a NEW TAB (spawn-or-attach at that path)
        -- a judgment call, flagged: this session's cwd is fixed once
        connected, so picking a different repo here must not mutate the
        running session out from under it. The least-surprising reading is
        the same one Ctrl+T / `doxa` in that directory already gives, so
        this calls the SAME spawn primitive (``DoxaApp.open_tab_at``,
        itself a thin wrapper over ``doxa.daemon.spawn_daemon``/
        ``SessionEngine`` -- the ONE spawn implementation every existing
        factory closure already wraps), never a second one."""
        self.run_worker(self._do_spawn_tab_at(path), group="tabs")

    async def _do_spawn_tab_at(self, path: str) -> None:
        error = await self.app.open_tab_at(path)
        if error:
            await self._system(f"repo: {error}")

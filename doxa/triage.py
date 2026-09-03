# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.triage -- what a rail entry IS, what state it is in, and what
colour its project wears.

Pure data and pure functions, the rule :mod:`doxa.layout` and
:mod:`doxa.collections` already follow: the parts of triage that are hard
to get right -- most-urgent-wins over the tabs a pane group hides, "old",
a colour that must be the same on every machine with nothing stored --
are the parts that must be testable without a running app. The rail
(:mod:`doxa.ui.sidebar`) renders what this module decides; the app
(:mod:`doxa.app`) reads the facts off the widgets and hands them here.

**Three channels, three jobs, and they are deliberately not the same
channel** (docs/plans/collection-triage.md, Part 1):

======================  ==========================  ====================
channel                 carries                     derived from
======================  ==========================  ====================
glyph                   *what state* -- urgency     the marks the tab
                                                    header already ORs,
                                                    plus ctx%
base colour             *which project* -- identity ``repo_root``
contrast (dim)          *how old*                   :data:`OLD_STATES`
======================  ==========================  ====================

Identity is stable and says nothing about urgency; state changes minute
to minute and says nothing about identity; age is neither. Painting two
of them into one hue is how a rail stops being readable -- a session
cannot be "the red project" and "red because it needs you" at once.

**The check the spec owes itself, answered here.** Can a project AND
every session's status be identified with colour stripped entirely? Yes,
and it is this module that makes it so: :func:`entry_glyphs` puts the two
interrupting states in two dedicated character columns, and the project's
own NAME is the heading a row sits under. Colour is redundancy on both.
tests/test_triage.py asserts exactly that with every style removed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .ui.labels import TAB_STATE_MARKS, sidebar_mark_glyph, top_mark

# -- Part 0: the two states worth interrupting for ---------------------
#
# Two, not five. A scale of glyphs is a gauge, and the ctx chip already
# is one (doxa.ui.labels.ctx_chip escalates through three tiers and keeps
# printing the number throughout). What a RAIL owes is a yes/no per row,
# in a column narrow enough that 22 columns still fit a label.

#: The context share at which a session earns its glyph. **A named
#: constant, never a literal**: it is the first threshold anyone will
#: want to tune, and a second one (75%? 90%?) is a plausible follow-up
#: that must not have to go hunting for a `50` in the middle of a render
#: method. Deliberately BELOW :data:`doxa.ui.labels.CTX_AMBER_PCT` (70):
#: the chip's amber is "start thinking about /compact" for the session
#: you are looking at, and the rail's job is the session you are NOT.
CTX_GLYPH_PCT = 50.0

#: The mark that means *needs input*: the session is stopped, waiting for
#: a human -- DOXA is idle *because of you*. NOT a second reading of what
#: that means: ``-attention`` is the class
#: :meth:`doxa.session.pane.SessionPane._set_tab_class` already writes
#: (and blinks at 2 Hz) and the tab header already carries. This module
#: names it; it does not decide it.
NEEDS_INPUT_MARK = "-attention"

#: *Needs input.* U+23F3 HOURGLASS WITH FLOWING SAND, Miscellaneous
#: Technical -- the block :data:`doxa.diff.REJECT_QUEUED`'s own ``⏳``
#: already ships in, so this introduces no new codepoint class and needs
#: no ``ascii`` fallback of its own. The banner work rejected Geometric
#: Shapes for tofu risk and v0.81.0's draughts glyphs (⛀⛁⛶) ship only
#: behind ``context_grid = ascii`` for the same reason; this glyph is
#: chosen so that neither cost is paid again.
GLYPH_NEEDS_INPUT = "⏳"

#: *Context at or past* :data:`CTX_GLYPH_PCT`. U+29C9 TWO JOINED SQUARES,
#: Miscellaneous Mathematical Symbols-B -- already shipping in
#: :func:`doxa.paste.placeholder`, the subagent chip and the transcript's
#: running-task row, all of which mean "more than one thing here". The
#: reading the rail wants is the same one, one level down: the window is
#: stacking up.
GLYPH_CTX = "⧉"

#: What a glyph column shows when it has nothing to say, so every label
#: starts in the same column whether or not its session has news. The
#: same posture :data:`doxa.ui.labels.SIDEBAR_MARK_NONE` takes one column
#: to the left.
GLYPH_NONE = " "

#: How many columns :func:`entry_glyphs` always returns. Fixed, because
#: :data:`doxa.layout.SIDEBAR_CHROME` prices it and a variable-width
#: prefix would put every label in a different column.
GLYPH_COLUMNS = 2


def needs_input(marks: Any) -> bool:
    """Is this session stopped, waiting for a human?"""
    return NEEDS_INPUT_MARK in set(marks or ())


def ctx_full(percentage: "float | None") -> bool:
    """Is this session at or past :data:`CTX_GLYPH_PCT`?

    **Unknown is not "no".** ``None`` is what
    :attr:`doxa.engine.Engine.last_ctx_percentage` holds for a session
    whose limit the CLI never reported, and it answers False here so that
    :func:`entry_glyphs` renders NOTHING rather than the absence of a
    warning -- which is the same fact one level down as ``/context``'s
    rule that an unreported limit reads ``?`` and stays ``?``. The two
    callers that could confuse them (this and :func:`urgency`) are the
    only two, and both are tested for it."""
    return percentage is not None and float(percentage) >= CTX_GLYPH_PCT


def entry_glyphs(marks: Any, percentage: "float | None") -> str:
    """Part 0's two columns, always exactly :data:`GLYPH_COLUMNS` wide.

    Column one is the state mark's own glyph
    (:func:`doxa.ui.labels.sidebar_mark_glyph`, whose ``-attention``
    entry IS :data:`GLYPH_NEEDS_INPUT`); column two is the ctx glyph or a
    space. Two independent columns rather than one winner, because the
    two states are independent facts: a session can be waiting for you
    AND half full, and a rail that showed only the more urgent of them
    would drop the one that is about to stop being recoverable."""
    return sidebar_mark_glyph(marks) + (
        GLYPH_CTX if ctx_full(percentage) else GLYPH_NONE
    )


# -- Part 3's ranking, borrowed early by Part 1b's aggregation ---------
#
# Part 3 (ordering collections by urgency) is NOT in this release. Its
# RANKING is, because Part 1b needs it: "an entry's state is the maximum
# urgency over its members". Shipping the ranking without the reordering
# is deliberate -- the ranking is what makes an entry honest, and the
# reordering is the part with the hazard (a list that moves under your
# click).


def urgency(marks: Any, percentage: "float | None" = None) -> int:
    """How loudly this session is asking for a human. Higher wins.

    **Not a second precedence table.**
    :data:`doxa.ui.labels.TAB_STATE_MARKS` is the one written-down
    statement of what outranks what -- doxa/theme.tcss cascades it, the
    tab strip paints it and :func:`doxa.ui.labels.top_mark` reads it --
    so the mark ranks here are that tuple's own index and nothing else.
    The ONE thing this function adds is where ctx% slots in, which that
    tuple cannot say because ctx is not a mark: **just under needs
    input**, because a session that is stopped waiting for you is the
    only state where DOXA is idle *because of you*, and everything else
    can wait behind it.

    ``docs/plans/collection-triage.md``'s Part 3 lists staged above
    working; ``TAB_STATE_MARKS`` puts working above staged. The tuple
    wins, and the deviation is recorded here rather than resolved by
    writing a second order that would then have to be kept in sync with
    the stylesheet. An unknown ctx contributes nothing at all -- see
    :func:`ctx_full`."""
    mark = top_mark(marks)
    if mark == NEEDS_INPUT_MARK:
        return len(TAB_STATE_MARKS) + 2
    if ctx_full(percentage):
        return len(TAB_STATE_MARKS) + 1
    return (TAB_STATE_MARKS.index(mark) + 1) if mark else 0


# -- Part 1: colour keyed to the PROJECT -------------------------------

#: The fixed palette, by NAME. Six, and small on purpose: a big palette
#: buys hues a terminal cannot separate, and the name -- not the colour
#: -- is the primary channel (see :func:`colour_for` on collisions).
#:
#: Every hue here is chosen against doxa/theme.tcss's existing ramp AND
#: against the four status colours it must not be confused with
#: (``-done-unseen`` #6FCF97, ``-staged`` #A98FD1, ``-working`` #E0A83C,
#: ``-attention`` #B23B32). A project that wore one of those would make
#: the identity channel read as a state.
#:
#: The NAME is what a config file stores and what a record would carry;
#: the hex lives once, in doxa/theme.tcss, as
#: ``SidebarLine.-project-<name>`` -- so a future theme change
#: re-resolves the colour instead of stranding one that no longer
#: contrasts. That is :mod:`doxa.collections`' "names are ids'
#: companions" rule applied to paint.
PALETTE: "tuple[str, ...]" = ("teal", "sky", "rose", "clay", "moss", "mauve")

#: What "no project colour" is called. **Grey means exactly one thing:
#: the absence of a project colour**, and nothing else
#: (docs/plans/collection-triage.md, Part 1b). A session outside a repo
#: has no project, so it has no colour, and grey is what "no colour"
#: looks like rather than a colour that means something. Age does NOT
#: recolour a row -- see :data:`OLD_STATES`.
NO_COLOUR = ""


def colour_for(
    repo_root: Any, override: "str | None" = None
) -> str:
    """The palette NAME for a project, or :data:`NO_COLOUR`.

    **Assigned, not configured.** Asking a user to pick a colour per repo
    is a chore that will not be done, and an unconfigured project would
    fall back to no colour -- which is where most projects would stay. So
    the colour is a stable hash of ``repo_root`` into :data:`PALETTE`:
    the same repo is the same colour on every machine and across
    restarts, with nothing stored anywhere.

    ``blake2b`` and not :func:`hash`, and that is the whole point of the
    line: CPython salts string hashing per process (``PYTHONHASHSEED``),
    so ``hash()`` would give one repo a different colour in every window
    of the same machine. A digest is the only thing that makes "nothing
    stored" and "the same everywhere" true at once.

    ``override`` is what :func:`doxa.config.project_colour` read out of
    ``~/.doxa/config.toml`` for this repo -- a palette NAME, never a hex.
    A name that is not in the palette is IGNORED rather than honoured: a
    hand-typed ``#3a3a3a`` is unreadable on half the terminals in the
    world, and a typo'd name must fall back to the assignment rather than
    silently uncolour the project.

    **Collision is expected and is not hidden.** Six names and a hash
    means two projects eventually share a colour. That costs redundancy,
    not meaning: the project's NAME is the primary channel and each
    project keeps its own heading, keyed by ``repo_root`` and never by
    colour -- so two same-coloured projects are two headings that happen
    to match, never one group. :func:`doxa.ui.sidebar.build_rows` groups
    by the root; nothing in the rail groups by the colour."""
    if override:
        name = str(override).strip().casefold()
        if name in PALETTE:
            return name
    root = str(repo_root or "").strip()
    if not root:
        return NO_COLOUR
    digest = hashlib.blake2b(root.encode("utf-8", "replace"), digest_size=8)
    return PALETTE[int.from_bytes(digest.digest(), "big") % len(PALETTE)]


def project_name(repo_root: Any) -> str:
    """The heading label for a project: its root's basename.

    The PRIMARY channel, and the reason the monochrome check passes for
    grouping. ``repo_root`` is what :attr:`doxa.peers.PeerInfo.scope_key`
    already keys on, so two sessions on one repo agree they are related
    before the rail is even asked."""
    root = str(repo_root or "").strip()
    if not root:
        return ""
    return Path(root).name or root


# -- Part 1b: age, as a channel of its OWN -----------------------------

#: A session with a live pane in this window.
STATE_LIVE = "live"

#: A session this window closed the pane on (``Ctrl+W``, ``/detach``)
#: while the session itself kept running.
STATE_DETACHED = "detached"

#: A session this window ENDED (``Ctrl+Q``) -- ``DoxaApp._ended_this_run``.
STATE_ENDED = "ended"

#: **What "old" means, decided.** ENDED, and nothing else.
#:
#: The request said "old sessions fade colour to grey" and the spec
#: refused to settle which fact that is, listing three candidates that
#: are genuinely different: ended, detached with no client, and idle for
#: N minutes with the session still live. This is the choice:
#:
#: * **IS old:** a session this window ended. It is over. Nothing will
#:   change about it again, and the row exists so the user can read the
#:   transcript, not so they can go back to work in it.
#: * **is NOT old: detached.** A detached session is LIVE and may be
#:   doing work right now -- that is the whole reason ``/detach``
#:   exists -- and dimming it would be the rail saying "nothing here"
#:   about the one row most likely to have something. It renders
#:   ``· closed`` (:data:`doxa.ui.sidebar.NOT_OPEN`) because its pane is
#:   gone; that is a statement about the WINDOW, not about the session.
#: * **is NOT old: idle for N minutes.** An attached session idle for an
#:   hour is one keystroke from being the thing you are doing. It is also
#:   the only candidate that needs a clock, and a dim that goes stale
#:   between refreshes is worse than no dim: the rail repaints on tab
#:   lifecycle events, not on a timer, and this feature is not worth
#:   reintroducing one (the busy-idle cost v1.0.0 measured).
#:
#: A frozenset rather than a comparison so that adding a second old state
#: later is a one-line edit in ONE place, and so that "detached is not
#: old" is visible as an absence a reader can check rather than a
#: condition they have to reconstruct.
OLD_STATES: "frozenset[str]" = frozenset({STATE_ENDED})


def is_old(state: Any) -> bool:
    """Is this session's state one the rail should DIM?

    Dim -- reduced contrast -- and never recolour: an old entry in a
    coloured project stays that project's colour, faded, which is what
    "fade to grey" actually describes and is what lets age compose with
    grouping instead of competing with it. See :data:`NO_COLOUR`."""
    return str(state or "") in OLD_STATES


# -- the facts the rail is handed --------------------------------------


@dataclass(frozen=True)
class Facts:
    """Everything the rail knows about ONE session, gathered once.

    Read off the widgets by :meth:`doxa.app.DoxaApp._describe_session`
    in the SAME single pass that already answered ``(label, marks,
    mounted)``, and for the reason that pass exists: ``panes()`` is a
    walk of the screen's widget tree, and v1.0.0 measured what asking it
    per row costs (+22% layout time). Nothing here is derived a second
    time -- the marks are the tab header's own, the ctx% is the number
    the status chip prints, the project is the root the git chip already
    resolved."""

    label: str = ""
    marks: "tuple[str, ...]" = ()
    mounted: bool = False
    #: The CLI's own accounting, or ``None`` for a session whose limit was
    #: never reported. ``None`` is not zero -- see :func:`ctx_full`.
    ctx_percentage: "float | None" = None
    state: str = STATE_LIVE
    #: ``repo_root``: the identity key, not a display string.
    repo_root: str = ""

    def urgency(self) -> int:
        return urgency(self.marks, self.ctx_percentage)


@dataclass(frozen=True)
class PaneEntry:
    """One rail entry: **a PANE GROUP, not a session.**

    Since v0.97.0 the window is a tree of :class:`doxa.ui.split.PaneGroup`
    and each group owns its own tab strip, so one visible pane can hold
    three sessions of which two are invisible. A rail that lists sessions
    flat cannot show that; a rail that lists panes can, and the pane is
    what the user actually navigates to.

    ``sessions`` is the group's tabs in STRIP order and ``active`` is the
    one on screen. A session with no pane behind it at all -- a detached
    peer, an ended one -- is its own single-member entry with an empty
    ``active``, which is the honest reading: there is no visible tab
    because there is no pane."""

    key: str
    sessions: "tuple[str, ...]" = ()
    active: str = ""


@dataclass(frozen=True)
class EntryState:
    """What one :class:`PaneEntry` shows, after aggregation.

    **Most urgent wins**, over every member including the ones no pixel
    of which is on screen -- that is the whole point of the change: the
    invisible tab needing input is exactly what a flat rail cannot
    surface.

    ``hidden`` is the half that keeps the rail honest. If the winning
    member is not the one on screen, a user who opens the pane sees a
    calm active tab and concludes the rail lied. So the entry NAMES the
    member the state came from (``label``/``session_id`` are that
    member's, and a click reveals it, not the group's active tab) and
    :attr:`position` says which of how many it is."""

    session_id: str = ""
    label: str = ""
    #: Every mark held by ANY member, in :data:`TAB_STATE_MARKS` order --
    #: the same OR over leaves the tab header does, one level up.
    marks: "tuple[str, ...]" = ()
    ctx_percentage: "float | None" = None
    count: int = 0
    #: 1-based index of the winning member in strip order, or 0 when it
    #: IS the visible tab (or when there is only one).
    position: int = 0
    hidden: bool = False
    old: bool = False
    repo_root: str = ""
    mounted: bool = True

    def glyphs(self) -> str:
        return entry_glyphs(self.marks, self.ctx_percentage)

    def count_chip(self) -> str:
        """How many tabs this entry holds, and -- when the state is not
        the visible tab's -- WHICH of them it came from.

        Plain digits and a middle dot, deliberately: the two glyph
        columns are the codepoint budget this feature spends, and a
        count is the one thing ASCII renders perfectly on every terminal
        there has ever been. ``·3`` is "three tabs, and what you see is
        what is on screen"; ``·2/3`` is "three tabs, and this is the
        second one -- the one you cannot see". Empty for a one-tab
        entry, which is the overwhelmingly common case and must look
        exactly as it did in v1.0.0 (hide at zero)."""
        if self.count <= 1:
            return ""
        if self.hidden and self.position:
            return f" ·{self.position}/{self.count}"
        return f" ·{self.count}"


def aggregate(
    entry: "PaneEntry", facts: "dict[str, Facts]"
) -> "EntryState | None":
    """One entry's state, from its members. ``None`` when it has none the
    rail can describe.

    Ties go to the VISIBLE tab and then to strip order, so the answer is
    stable rather than arbitrary: a rail whose entry renamed itself
    between two identical refreshes would be a rail nobody trusts, which
    is :class:`doxa.collections.Collection`'s own reason for storing an
    order rather than a set."""
    members = [
        (index, session_id, facts[session_id])
        for index, session_id in enumerate(entry.sessions)
        if session_id in facts and facts[session_id].label
    ]
    if not members:
        return None
    def rank(item: "tuple[int, str, Facts]") -> tuple:
        index, session_id, fact = item
        return (fact.urgency(), session_id == entry.active, -index)

    winner_index, winner_id, winner = max(members, key=rank)
    marks = tuple(
        name for name in TAB_STATE_MARKS
        if any(name in fact.marks for _i, _s, fact in members)
    )
    # The loudest ctx over the members, unknowns skipped entirely -- an
    # unreported limit contributes nothing rather than a zero.
    known = [
        fact.ctx_percentage for _i, _s, fact in members
        if fact.ctx_percentage is not None
    ]
    hidden = bool(entry.active) and winner_id != entry.active
    return EntryState(
        session_id=winner_id,
        label=winner.label,
        marks=marks,
        ctx_percentage=max(known) if known else None,
        count=len(members),
        position=(winner_index + 1) if hidden else 0,
        hidden=hidden,
        old=all(is_old(fact.state) for _i, _s, fact in members),
        repo_root=(
            facts[entry.active].repo_root if entry.active in facts
            else winner.repo_root
        ),
        mounted=winner.mounted,
    )


def entries_for(
    order: "Sequence[str]", panes: "Sequence[PaneEntry]"
) -> "list[PaneEntry]":
    """Every session in ``order`` folded into pane entries, in the order
    their FIRST member appears.

    A session the caller gave no pane group for -- detached, ended,
    archived -- becomes a single-member entry of its own, keyed by its
    session id. That is not a special case bolted on: it is the same
    statement :func:`doxa.ui.sidebar.build_rows` has made since v1.0.0,
    that a rail is a session INDEX and not a second tab strip, restated
    now that an entry is a pane."""
    known = [s for s in order if s]
    held: "dict[str, PaneEntry]" = {}
    for entry in panes:
        for session_id in entry.sessions:
            held.setdefault(session_id, entry)
    out: "list[PaneEntry]" = []
    seen: "set[str]" = set()
    for session_id in known:
        entry = held.get(session_id)
        if entry is None:
            entry = PaneEntry(session_id, (session_id,), "")
        if entry.key in seen:
            continue
        seen.add(entry.key)
        out.append(
            PaneEntry(
                entry.key,
                tuple(s for s in entry.sessions if s in known),
                entry.active if entry.active in known else "",
            )
        )
    return out

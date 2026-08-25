"""doxa.ui.beliefs -- lettered item V: the beliefs browser.

WHAT THIS IS, AND WHAT IT IS NOT. v0.27.0 shipped a lightweight beliefs
PICKER on the status-bar chip and said in its own docstring that item V
still owned the real browser -- evidence trails, approve/reject. This is
that browser. The picker stays exactly where it was: it is the glance, one
dropdown, no writes; this is the session you open when the glance was not
enough, and the picker's first row is the door to it.

Both, rather than one replacing the other, because they answer different
questions. "What does LORE believe about me, roughly?" is a dropdown.
"Which of these 619 beliefs is stale, what was this one derived from, and
should these 166 staged proposals be applied?" is not.

A TAB, not a modal and not a dropdown. ``SubagentTranscriptTab`` and
``ArchivedSessionTab`` are the house precedents for a non-session tab and
this follows them exactly: a plain ``TabPane`` in the same ``#session-tabs``
strip, no engine of its own, no prompt. Full height, which is the point --
a ten-row picker cannot make a hundred and sixty proposals reviewable, and
the verdict column exists precisely so they can be reviewed. Reviewing is
a task with a beginning and an end; a modal that blocks the session while
you do it is the wrong shape, and one that closes when you glance away
loses your place.

The surface divides its space with a FIXED split (a header block, then one
scrolling body) and has no drag handle. Draggable dividers between the
transcript, the prompt and the status bar are a general layout capability
that belongs to the recursive split-panes work (docs/split-panes.md), and
bolting a one-off resizer into one browser is how a layout system ends up
with two of them that behave differently.

WHY A WIDGET PER ROW. Three of item V's requirements are impossible on an
``OptionList``, which is what the chip picker is:

* **Per-row tooltips.** A tooltip is a ``Widget`` attribute; an ``Option``
  has none. The user asked for the full claim text on hover, and the only
  way to give a hundred rows a hundred different tooltips is a hundred
  widgets. It also sidesteps the v0.35.0 ctx-chip defect by construction:
  that hint vanished because it was KEYED by the chip's markup while the
  lookup ran against markup-stripped text. There is no lookup here. The
  row object that renders the line is the row object that carries the
  tooltip, set from the same record, in the same constructor -- the same
  discipline ``StatusChip`` applies to markup and hints.
* **Two click targets in one row.** ``OptionList`` dispatches per OPTION,
  so approve and reject inside one option are one target.
* **Expanding a row in place.** An evidence trail mounts under its belief
  and is removed again; an option list has nowhere to put it.

MISCLICK ASYMMETRY, and how it is resolved here. Approve WRITES into
curated memory or the belief store -- material that is injected into the
model's context on every prompt. Reject archives a JSON file that stays on
disk. They are not equally recoverable, so they are not equally easy:

* **reject** is one click (or ``r``) and takes effect immediately.
* **approve** ARMS on the first click (or ``a``) and applies on the
  second, on that same row, with the armed control repainted in a
  different colour and different words. It is not a modal -- the whole
  point of a per-row button is not having one -- and it is not a
  confirmation dialog you dismiss; it is the same control, said twice, in
  place. Arming any row disarms every other, so there is never more than
  one armed control on screen.
* the two controls are visually distinct and spatially separated, and
  neither is bound to Enter. Enter expands a row's detail and nothing
  else, because Enter is what a hand rests on.

Nothing here approves without an explicit action ON ONE ROW. There is no
"approve all", no multi-select, no default-focused approve button. The
engine method behind it takes one id and there is no list form to add one
to -- see ``SessionEngine.approve_pending``.

READ-ONLY DEGRADATION. When the loaded ``lore_core`` cannot record an
approval honestly (anything before LORE 0.36.0's write gate and provenance
ledger, and DOXA can be pointed at one -- the Claude Code plugin checkout
wins over the pinned wheel), the browser says so in a banner and renders
no approve/reject controls at all. See ``doxa.engine.lore_write_state``.
"""

from __future__ import annotations

import contextlib
import inspect
from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static, TabPane

from .labels import (
    CLICKABLE_CHIP_ACCENT,
    CTX_AMBER,
    CTX_RED,
    _belief_scope_label,
    _escape_markup,
    _one_line,
    as_proposal,
    belief_created_text,
    belief_outcome_color,
    belief_outcome_text,
    belief_provenance,
    belief_sort_key,
    belief_tooltip,
    ellipsize,
    proposal_age_text,
    proposal_supersedes,
    proposal_text,
    proposal_tooltip,
    proposal_verdict,
)

#: The tab's own label. Carries a mark like ``ArchivedSessionTab``'s ``⏺``
#: so a strip of tabs says at a glance which one is not a session.
BROWSER_MARK = "◈"

#: How wide a claim line runs before it is ellipsized. The tooltip carries
#: the whole thing (that is the user's ask), so this is a reading width,
#: not a truncation policy.
CLAIM_WIDTH = 110

#: The armed-approve colour. Deliberately NOT the accent every other
#: clickable span in this app wears -- an armed irreversible control must
#: not look like an ordinary affordance.
ARMED_COLOR = CTX_RED
#: Reject: present, reachable, and visibly not the same thing as approve.
REJECT_COLOR = CTX_AMBER


def _span(text: str, action: str, color: str) -> str:
    """One clickable control inside a row -- the same unprefixed
    ``[@click=...]`` markup span :class:`doxa.ui.transcript.SystemBlock`
    and the status bar already use, resolved against the clicked widget
    itself by ``Widget.broker_event``, which is why the ``action_*``
    methods live on the row classes below and need no ``app.``/``screen.``
    prefix."""
    return f"[@click={action}][{color}]{_escape_markup(text)}[/][/]"


class BrowserNote(Static):
    """A heading, a caveat, or the read-only banner. Never a destination."""

    def __init__(self, text: str, *, classes: str = "") -> None:
        super().__init__(text, classes=f"beliefs-note {classes}".strip())


class EvidenceTrail(Static):
    """One belief's evidence, mounted under its row while it is expanded.

    Its own widget rather than more lines inside the row, because it is
    fetched separately (lazily, per belief -- see
    ``SessionEngine.belief_evidence``) and arrives after the row is
    already on screen."""

    def __init__(self, belief_id: Any, rows: "list[dict]") -> None:
        self.belief_id = belief_id
        super().__init__(self._render_rows(rows), classes="belief-evidence")

    @staticmethod
    def _render_rows(rows: "list[dict]") -> str:
        if not rows:
            return (
                "    no evidence rows — this belief carries no derivation "
                "trail in the store"
            )
        lines = []
        for row in rows:
            when = str(row.get("created") or "?")
            session = str(row.get("session_id") or "?")
            project = str(row.get("project") or "")
            note = _one_line(str(row.get("note") or ""), 200)
            head = f"    {when}  session {session}"
            if project:
                head += f"  [{project}]"
            lines.append(_escape_markup(head))
            if note:
                lines.append(_escape_markup(f"        {note}"))
            if row.get("note_truncated"):
                lines.append("        [note truncated — larger than one wire frame]")
            if row.get("trail_truncated"):
                lines.append(
                    "    … trail continues — more evidence than one page holds"
                )
        return "\n".join(lines)


class BrowserRow(Static):
    """Shared behaviour of the two row kinds: focusable, keyboard-navigable,
    and carrying its own tooltip.

    ``up``/``down`` move focus between rows rather than leaving the browser,
    because a list you can only leave with Tab is not a list."""

    can_focus = True

    BINDINGS = [
        Binding("up", "row_prev", "Previous row", show=False),
        Binding("down", "row_next", "Next row", show=False),
    ]

    def __init__(self, browser: "BeliefsBrowserTab", **kwargs: Any) -> None:
        self.browser = browser
        super().__init__(**kwargs)

    def _move(self, delta: int) -> None:
        rows = self.browser.rows
        if self not in rows:
            return
        index = rows.index(self) + delta
        if 0 <= index < len(rows):
            rows[index].focus()
            with contextlib.suppress(Exception):
                rows[index].scroll_visible()

    def action_row_prev(self) -> None:
        self._move(-1)

    def action_row_next(self) -> None:
        self._move(1)

    def on_focus(self, event: events.Focus) -> None:
        self.add_class("-row-focused")

    def on_blur(self, event: events.Blur) -> None:
        self.remove_class("-row-focused")


class BeliefRow(BrowserRow):
    """One active belief: scope, confidence, when it was created, WHAT
    REALITY LAST SAID ABOUT IT, its provenance, its evidence count, and
    the claim.

    That middle column is the v0.46.0 correction. It used to be "40d
    idle" -- how long since anything touched the belief -- and touching is
    not testing. It is now the newest row of LORE's own
    ``belief_outcomes`` ledger: ``confirmed 2d``, ``contradicted 2d``,
    ``stale 40d``, each in its own colour because the verdicts are
    opposite facts, or the plain word ``never tested`` in the muted body
    colour, which is what ~95% of a real store says.

    Read-only, always. Item V's write half is the staged-proposal queue --
    a belief already IN the store is not something this browser edits, and
    a retraction is itself a proposal (``lore belief retract`` stages one,
    and it shows up below as ``retract → belief #N``).

    Enter expands the evidence trail. It is the only thing Enter does
    anywhere in this browser."""

    BINDINGS = [Binding("enter", "toggle_evidence", "Evidence", show=False)]

    def __init__(self, browser: "BeliefsBrowserTab", belief: dict) -> None:
        self.belief = dict(belief)
        self.belief_id = belief.get("id")
        self._trail: "EvidenceTrail | None" = None
        super().__init__(browser, classes="belief-row")
        self.update(self._line())
        # The tooltip is set from the SAME record, in the SAME constructor
        # that built the line -- see this module's docstring on why that
        # is written down rather than left to a helper somewhere else.
        self.tooltip = belief_tooltip(self.belief)

    def _line(self) -> str:
        b = self.belief
        subject = str(b.get("subject") or "")
        confidence = b.get("confidence")
        conf = f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "?"
        count = b.get("evidence_count")
        # Everything except the outcome is plain, escaped text; the outcome
        # is the ONE coloured token on the line, because it is the one a
        # reader is scanning for and because "confirmed" and "contradicted"
        # differing only by a word is not enough of a difference.
        lead = [
            _belief_scope_label(subject),
            f"conf {conf}",
            belief_created_text(b) or "created ?",
        ]
        tail = [
            belief_provenance(b),
            f"{count} evidence" if isinstance(count, int) else "",
        ]
        segments = [_escape_markup(" · ".join(p for p in lead if p))]
        outcome = belief_outcome_text(b)
        if outcome:
            colour = belief_outcome_color(b)
            painted = _escape_markup(outcome)
            segments.append(f"[{colour}]{painted}[/]" if colour else painted)
        segments.append(_escape_markup(" · ".join(p for p in tail if p)))
        claim = ellipsize(_one_line(str(b.get("claim") or ""), 400), CLAIM_WIDTH)
        mark = "▾ hide evidence" if self._trail is not None else "▸ evidence"
        return (
            _span(mark, "click_toggle", CLICKABLE_CHIP_ACCENT) + "  "
            + " · ".join(seg for seg in segments if seg)
            + "\n    " + _escape_markup(claim)
        )

    def _repaint(self) -> None:
        self.update(self._line())

    async def action_toggle_evidence(self) -> None:
        """Fetch and show this belief's derivation trail, or hide it again.

        Fetched on EXPAND, never on load: the belief page carries an
        evidence COUNT and nothing more, which is how a browser over
        hundreds of beliefs stays inside one 64KB wire frame."""
        if self._trail is not None:
            trail, self._trail = self._trail, None
            with contextlib.suppress(Exception):
                await trail.remove()
            self._repaint()
            return
        rows = await self.browser.fetch_evidence(self.belief_id)
        trail = EvidenceTrail(self.belief_id, rows)
        parent = self.parent
        if parent is None:
            return
        await parent.mount(trail, after=self)
        self._trail = trail
        self._repaint()

    def action_click_toggle(self) -> None:
        self.run_worker(self.action_toggle_evidence(), group="beliefs-browser")


class ProposalRow(BrowserRow):
    """One staged proposal: what approving it would DO, how long it has
    waited, its text, and -- when the loaded lore_core can record an
    approval honestly -- its own approve and reject controls.

    The verdict leads the row because it is the part a reviewer scans, and
    because a row that does not say what approving it changes is not
    reviewable. That was item V's brief in one sentence.

    Arming: ``a`` (or a click on approve) arms; a second ``a`` (or a second
    click) applies. ``r`` (or a click on reject) discards. Enter does
    neither -- see the module docstring."""

    BINDINGS = [
        Binding("a", "approve", "Approve (twice)", show=False),
        Binding("r", "reject", "Reject", show=False),
        Binding("escape", "disarm", "Disarm", show=False),
    ]

    def __init__(
        self, browser: "BeliefsBrowserTab", item: "dict | str", *, writable: bool,
    ) -> None:
        self.proposal = as_proposal(item)
        self.pid = str(self.proposal.get("pid") or "")
        self.writable = bool(writable and self.pid)
        self.armed = False
        self.resolved = ""
        self._busy = False
        super().__init__(browser, classes="proposal-row")
        self.update(self._line())
        self.tooltip = proposal_tooltip(self.proposal)

    def _controls(self) -> str:
        if self.resolved:
            return ""
        if not self.writable:
            return ""
        if self.armed:
            # Different words AND a different colour from the unarmed
            # control: an armed irreversible action must not be able to be
            # mistaken for the affordance that armed it.
            return (
                "        "
                + _span("✓ CONFIRM APPROVE", "approve", ARMED_COLOR)
                + "     " + _span("✗ reject", "reject", REJECT_COLOR)
                + "        (Esc, or any other row, disarms)"
            )
        return (
            "        " + _span("✓ approve", "approve", CLICKABLE_CHIP_ACCENT)
            + "     " + _span("✗ reject", "reject", REJECT_COLOR)
        )

    def _line(self) -> str:
        item = self.proposal
        verdict = proposal_verdict(item) or "verdict unavailable (no record)"
        age = proposal_age_text(item)
        supersedes = proposal_supersedes(item)
        head_parts = [verdict]
        if supersedes:
            head_parts.append(f"supersedes: {ellipsize(supersedes, 46)}")
        if age:
            head_parts.append(age)
        if item.get("writer"):
            head_parts.append(f"!! write-gate staged ({item['writer']})")
        if item.get("cross_project_note"):
            head_parts.append("!! cross-project")
        head = _escape_markup(" · ".join(head_parts))
        if self.resolved:
            head = f"{self.resolved} · {head}"
        text = ellipsize(_one_line(proposal_text(item), 400), CLAIM_WIDTH)
        return f"{head}{self._controls()}\n    " + _escape_markup(text)

    def _repaint(self) -> None:
        self.update(self._line())
        self.tooltip = proposal_tooltip(self.proposal)

    def disarm(self) -> None:
        if self.armed:
            self.armed = False
            self._repaint()

    def action_disarm(self) -> None:
        self.disarm()

    def action_approve(self) -> None:
        """First call ARMS this row. Second call on the SAME row applies.

        The arm is per row and exclusive -- arming disarms every other row
        -- so there is never a second armed control anywhere on screen for
        a stray click to land on."""
        if self.resolved or self._busy or not self.writable:
            return
        if not self.armed:
            self.browser.disarm_all(except_row=self)
            self.armed = True
            self._repaint()
            return
        self.armed = False
        self._busy = True
        self._repaint()
        self.run_worker(self.browser.apply(self, "approve"), group="beliefs-write")

    def action_reject(self) -> None:
        if self.resolved or self._busy or not self.writable:
            return
        self.armed = False
        self._busy = True
        self._repaint()
        self.run_worker(self.browser.apply(self, "reject"), group="beliefs-write")

    def settle(self, outcome: str) -> None:
        """Record what happened to this proposal, in the row itself.

        The row is NOT removed. A queue that silently loses the line you
        just acted on gives you nothing to check your own action against,
        and a browser whose whole premise is auditability should be the
        last surface to do that."""
        self._busy = False
        self.armed = False
        self.resolved = outcome
        self._repaint()


class BeliefsBrowserTab(TabPane):
    """Item V's surface: every active belief and every staged proposal, in
    one full-height tab.

    Not a ``SessionPane``: no engine of its own, no prompt -- it borrows
    the pane that opened it to reach that pane's engine, exactly as
    :class:`doxa.ui.transcript.SubagentTranscriptTab` borrows its owner.
    One browser per pane; opening it again brings the existing one
    forward rather than stacking a second."""

    def __init__(self, owner: Any, *, id: "str | None" = None) -> None:
        self.owner = owner
        self.rows: "list[BrowserRow]" = []
        self.write_state: dict = {}
        self.scroll = VerticalScroll(classes="beliefs-scroll")
        super().__init__(f"{BROWSER_MARK} beliefs", id=id)

    def compose(self) -> ComposeResult:
        yield self.scroll

    async def on_mount(self) -> None:
        self.run_worker(self.reload(), exclusive=True, group="beliefs-browser")

    # -- engine access -------------------------------------------------
    #
    # Every call goes through getattr on the pane's engine and tolerates
    # its absence, because the pane may hold either a SessionEngine or an
    # EngineClient (the daemon split) and, during teardown, neither. The
    # two agree on these names by design; a handle that does not answer to
    # one of them produces a row that says so, never a traceback.

    def _engine(self) -> Any:
        return getattr(self.owner, "engine", None)

    async def _ask(self, name: str, *args: Any, **kwargs: Any) -> Any:
        engine = self._engine()
        func = getattr(engine, name, None) if engine is not None else None
        if func is None:
            return None
        result = func(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    async def fetch_evidence(self, belief_id: Any) -> "list[dict]":
        try:
            return list(await self._ask("belief_evidence", belief_id) or [])
        except Exception:  # noqa: BLE001 -- an empty trail is rendered as one
            return []

    # -- rendering -----------------------------------------------------

    async def reload(self) -> None:
        """Read the store and paint the whole surface. Never raises: a
        browser that cannot read one of its two halves still shows the
        other, and says which one it lost."""
        await self.scroll.remove_children()
        self.rows = []
        try:
            state = await self._ask("lore_write_state") or {}
        except Exception as exc:  # noqa: BLE001
            state = {"capable": False, "reason": f"{type(exc).__name__}: {exc}"}
        self.write_state = dict(state)
        writable = bool(state.get("capable"))

        version = state.get("version") or "unknown version"
        source = state.get("source") or "unknown source"
        await self.scroll.mount(BrowserNote(
            f"◈ beliefs browser — LORE's memory for this session, and what is "
            f"waiting for your approval.\n"
            f"lore_core {version} ({source}).  ↑/↓ move · Enter expands a "
            f"belief's evidence · hover any row for its full text.",
            classes="beliefs-header",
        ))
        if not writable:
            # The mandatory read-only degradation, said where the controls
            # would have been rather than in a log nobody reads.
            await self.scroll.mount(BrowserNote(
                "read-only — " + _escape_markup(str(state.get("reason") or
                                  "this session cannot approve or reject")),
                classes="beliefs-readonly",
            ))

        await self._mount_proposals(writable)
        await self._mount_beliefs()
        if self.rows:
            self.rows[0].focus()

    async def _mount_proposals(self, writable: bool) -> None:
        try:
            proposals = list(await self._ask("list_pending") or [])
        except Exception as exc:  # noqa: BLE001 -- a refusal is information
            await self.scroll.mount(BrowserNote(_escape_markup(
                f"staged proposals could not be read — {type(exc).__name__}: {exc}"
            ), classes="beliefs-section"))
            return
        head = (
            f"▎ staged proposals ({len(proposals)}) — what each would do if "
            "approved" if proposals else
            "▎ staged proposals (0) — nothing is waiting for your approval"
        )
        if proposals and writable:
            head += "\n   a arms approve, a again applies · r rejects · Esc disarms"
        widgets: "list[Any]" = [BrowserNote(head, classes="beliefs-section")]
        for item in proposals:
            row = ProposalRow(self, item, writable=writable)
            widgets.append(row)
            self.rows.append(row)
        # ONE mount call, not one per row. This operator's queue holds 166
        # proposals and the belief list below holds 619; awaiting a mount
        # per row yields to the event loop (and re-lays-out) that many
        # times, which is the difference between a browser that opens and
        # one that visibly builds itself.
        await self.scroll.mount_all(widgets)

    async def _mount_beliefs(self) -> None:
        try:
            beliefs = list(await self._ask("list_beliefs") or [])
        except Exception as exc:  # noqa: BLE001
            await self.scroll.mount(BrowserNote(_escape_markup(
                f"beliefs could not be read — {type(exc).__name__}: {exc}"
            ), classes="beliefs-section"))
            return
        # Grouped by scope, the SAME grouping the chip picker uses; and
        # INSIDE a group, ordered by what reality has said (v0.46.0 --
        # see doxa.ui.labels.belief_sort_key). Tested beliefs first,
        # most recently tested first; never-tested after them as a bucket,
        # keeping list_beliefs' own updated-DESC order because Python's
        # sort is stable. With 31 outcome rows against 628 beliefs the
        # tested ones are needles, and a list that scattered them through
        # six hundred untested claims would have hidden the only evidence
        # it holds.
        ordered = sorted(
            beliefs,
            key=lambda b: (_belief_scope_label(str(b.get("subject") or "")),
                           belief_sort_key(b)),
        )
        widgets: "list[Any]" = [BrowserNote(
            f"▎ active beliefs ({len(ordered)})", classes="beliefs-section",
        )]
        current = None
        for belief in ordered:
            group = _belief_scope_label(str(belief.get("subject") or ""))
            if group != current:
                current = group
                widgets.append(BrowserNote(f"  ▎ {group}", classes="beliefs-group"))
            row = BeliefRow(self, belief)
            widgets.append(row)
            self.rows.append(row)
        await self.scroll.mount_all(widgets)  # one call -- see _mount_proposals

    # -- the write half ------------------------------------------------

    def disarm_all(self, except_row: "ProposalRow | None" = None) -> None:
        for row in self.rows:
            if isinstance(row, ProposalRow) and row is not except_row:
                row.disarm()

    async def apply(self, row: "ProposalRow", action: str) -> None:
        """Run ONE approve or reject, for ONE row, and say what happened.

        Neither outcome is silent: the row settles into a resolved state
        that names what was done, AND the owning pane gets a system block,
        because the browser tab may not be the tab the user is looking at
        by the time a write lands.

        The row list is never rebuilt here. Reloading the surface from
        inside a row's own callback is exactly the shape v0.27.0 hit a
        Textual race on (a picker reopened from a row callback raced the
        queued Blur and closed itself); settling the row in place has no
        such window and keeps the reviewer's position in a 166-row queue."""
        method = "approve_pending" if action == "approve" else "reject_pending"
        try:
            error = await self._ask(method, row.pid)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        verdict = proposal_verdict(row.proposal) or row.pid
        if error:
            row.settle("✗ NOT applied")
            await self._say(f"beliefs browser · {row.pid} — {_escape_markup(str(error))}")
            return
        if action == "approve":
            row.settle("✓ approved")
            await self._say(
                f"beliefs browser · approved {row.pid} — {verdict}. "
                "LORE recorded it as an approved write (via approved)."
            )
        else:
            row.settle("✗ rejected")
            await self._say(
                f"beliefs browser · rejected {row.pid} — {verdict}. "
                "Nothing was written; the proposal is archived."
            )

    async def _say(self, text: str) -> None:
        say = getattr(self.owner, "_system", None)
        if say is None:
            return
        with contextlib.suppress(Exception):
            await say(text)

# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.ui.dialogs -- the popups and modal screens a pane opens.

Extracted from ``doxa/app.py`` unchanged: the slash-command dropdown, the
needs-input popup, the shared status-chip picker, the two confirmations
(close-with-a-turn-running, compact), the tab rename bar, and the belief
inspector panel.

"Dialog" here means a surface the user answers, not a Textual
``ModalScreen`` specifically -- three of these are ordinary widgets mounted
above the prompt, because in a terminal the block list simply gives up the
rows, which reads as an overlay without the layer bookkeeping a floating
panel over a ``TabbedContent`` would need.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import Any, Callable  # noqa: F401 -- annotation-only

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.fuzzy import Matcher
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import commands as commands_mod
from .. import version as version_mod
from ..history import DEBOUNCE_SECS
from .labels import (
    PICKER_ROW_WIDTH,
    PICKER_TEXT_CAP,
    _escape_markup,
    ellipsize,
)


@dataclass(frozen=True)
class RowAction:
    """One inline action a beliefs/proposals chip-picker row carries --
    e.g. proposals' ``approve``/``reject``, beliefs' ``confirmed``/
    ``contradicted``/``stale``/``retract`` (v0.67.0: these used to live one
    selection deep, behind a per-row action SUB-menu -- see
    ``PaneChipsMixin._open_belief_actions``/``_open_pending_actions``, both
    still there and still reachable by selecting a row outright; this is
    the faster, inline path the row's own text now also carries).

    ``key`` is the bare keyboard letter this fires on (routed through
    :class:`doxa.ui.prompt.PromptInput` -- see :meth:`ChipPicker.
    try_action_key` -- rather than caught here directly, because the
    picker does not hold real focus while in ``prompt_filter`` mode).
    ``arms`` marks the two destructive/high-value verbs (``approve``,
    ``retract``) that take a SECOND press, on the SAME row, to actually
    fire -- approving writes into curated memory or the belief store,
    material injected into the model's context on every prompt, and
    retracting takes a belief out of the working set; neither is equally
    recoverable to a plain click, so neither is equally easy. Enforced by
    :attr:`ChipPicker._armed_rid` rather than a per-row ``armed`` flag
    (there is only ever one list open at a time here, so one id is the
    whole state)."""

    key: str
    verb: str
    label: str
    armed_label: str = ""
    color: str = ""
    armed_color: str = ""
    arms: bool = False

    @property
    def column_width(self) -> int:
        """Padded to the wider of its two wordings, so a row does not
        shift its neighbours' columns the moment it (and only it) arms."""
        return max(len(self.label), len(self.armed_label or self.label))


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


def _spawn_heading(data: dict) -> str:
    """The whole spawn confirmation as one renderable block: the title,
    what starts happening (already composed engine-side, from the numbers
    the caps themselves checked), and the child's task text VERBATIM.

    Verbatim is the point. ``doxa.session_ops`` caps the task at
    :data:`doxa.session_ops.MAX_TASK_CHARS` precisely so that this block
    is readable in full rather than needing an ellipsis here -- the
    review this dialog performs is the actual containment, and a reviewer
    who is shown a summary reviewed the summary."""
    title = str(data.get("title") or "start a second DOXA session?")
    body = str(data.get("body") or "")
    task = str(data.get("task") or "")
    parts = [title]
    if body:
        parts.append(body)
    if task:
        parts.append("\n".join(f"  │ {line}" for line in task.splitlines()))
    return "\n\n".join(parts)


class NeedsInputPopup(OptionList):
    """The needs-input dialog (queue item 5): same mount position, same
    "never takes focus" discipline as SlashComplete/SessionSearch -- above
    the prompt, driven entirely through :class:`PromptInput`'s key
    protocol. It serves every interactive case ``doxa.engine``'s
    ``needs_input`` event carries: an ``AskUserQuestion`` (one or more
    questions, answered one at a time -- multi-select collapses to the
    single highlighted/numbered choice; a model asking for more than one
    pick per question is rare enough that the SDK's own comma-joined-
    answer convention degrades gracefully to "just that one"), a plain
    permission request (tool name + input summary, Allow/Deny), and the
    ``spawn`` confirmation (v1.3.0) -- the same two-row Allow/Deny shape,
    with a multi-line heading carrying the child session's literal task
    text; see :func:`_spawn_heading`.

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
        elif self.kind == "spawn":
            # The spawn confirmation (v1.3.0, doxa.session_ops). Same two
            # rows and the same answer payload as a permission request --
            # it IS one -- but its heading is a BLOCK, not a line, and
            # that difference is the containment argument rather than a
            # cosmetic one: docs/plans/spawn-session.md rests on a human
            # reading the literal text the child will be given, so a
            # truncated one-line summary of it would be the dialog
            # quietly failing at its only job. Structurally this is still
            # one disabled Option at row 0 (see _render's contract) --
            # the block is one multi-line renderable, never extra rows,
            # so the number keys and the arrow math below are untouched.
            self._questions = []
            self._rows = [{"label": "start the session"}, {"label": "Deny"}]
            self._render(_spawn_heading(data), self._rows)
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
    on the CURRENT session (connect-time only, same as `/effort` itself).

    ``groups`` (item 3, the beliefs picker) is the same disabled-separator-
    row convention doxa.palette.DoxaPalette._refresh_command_list already
    established for the command palette's own section headers: an optional
    `rid -> group label` map, rendered as a dim `▎ <group>` row (the same
    `▎` marker CloseWithTurnRunning and the belief-inspector stub already
    use for a section lead-in) whenever the group changes, walking `rows`
    in the ORDER THE CALLER GAVE THEM -- grouping is the caller's job
    (sorting/bucketing its own rows), this widget only inserts the
    headers. A typed filter COLLAPSES the groups, same as the palette's
    own filtered search drops its section headers: once a fuzzy match is
    doing the ranking, a header contributes nothing a hit doesn't already
    say better.

    ``collapsible`` (v0.48.0) turns those headers into the only thing a
    large list shows until you ask for more: each becomes a SELECTABLE row
    carrying a fold marker and its own count -- `▸ project (412 beliefs)` --
    and selecting it folds that group open or shut in place. Requested
    against a store with 635 active beliefs, where a picker that opens
    fully expanded is a wall rather than a glance.

    Two properties of that are worth stating because they are load-bearing
    rather than incidental:

    * **Filtering is untouched by collapse, by construction.** The fold is
      applied ONLY inside the `grouped` branch below, and `grouped` is
      already false whenever a filter is typed -- the matcher has always
      scored `self._all_rows`, the complete set, not what is on screen. So
      a user typing a word still finds a belief inside a folded group, and
      it needs no auto-expand rule, no re-fold bookkeeping and no special
      case: the filtered view simply never had groups to fold. Clearing
      the filter returns to exactly the fold state that was there before.
    * **A small list does not fold at all.** Folding three rows behind
      three headers is strictly worse than showing them, so when the whole
      list already fits the widget (see :data:`AUTOEXPAND_ROWS`, which is
      `#chip-picker`'s own `max-height` in theme.tcss) every group opens
      and the feature is invisible."""

    can_focus = True
    BINDINGS = [Binding("escape", "close_picker", "Close", show=False)]

    #: Row-id prefix a FOLD HEADER carries. Namespaced so it cannot collide
    #: with a caller's own ids (`belief:7`, `pending:3`, a branch name), and
    #: checked by :meth:`select_row` before anything is handed to the
    #: caller's callback -- a header is this widget's own affordance and
    #: must never reach `on_select` as if it were a candidate.
    GROUP_ROW_PREFIX = "\x00group:"

    #: Columns this widget spends before a row's text starts: the leading
    #: space, the `▸` current-marker and the space after it. Subtracted
    #: from the MEASURED content width, never from a guess at the
    #: terminal's -- v0.49.0's banner work already paid for guessing
    #: chrome, and a scrollbar moves the budget by two.
    ROW_CHROME_COLS = 3

    #: Below this many candidate rows every group opens and the fold is
    #: invisible. It is `#chip-picker`'s own `max-height` in theme.tcss: a
    #: list the widget can already show at once gains nothing from being
    #: folded, and folding it would cost a selection to see what was
    #: previously just there.
    AUTOEXPAND_ROWS = 10

    def __init__(self, pane: "SessionPane") -> None:
        super().__init__(id="chip-picker")
        self.pane = pane
        self.display = False
        self._all_rows: list[tuple[str, str]] = []
        # Row-by-row map onto what the OptionList shows -- SAME
        # convention SlashComplete._rows follows, including the note
        # heading occupying index 0 as `("", note_text)` when present, and
        # any group header row inserted by `groups` (also `("", header)`).
        self._rows: list[tuple[str, str]] = []
        self._note = ""
        #: v0.69.0: the one column-name header the beliefs/proposals
        #: menus show (see :func:`doxa.ui.labels.format_picker_column_header`)
        #: -- a caller-supplied, ALREADY fixed-width string. Rendered like
        #: the note row (disabled, rid ``""``, so it is skipped by every
        #: existing rid-based guard with no changes) but its own thing,
        #: not folded into ``_note``: the note is a caveat about the LIST
        #: (a cap, a read-only reason) and can be absent; the header names
        #: what the columns ARE and is either always there or never
        #: relevant, never conditional on the list's own state.
        self._column_header: "str | None" = None
        self._groups: "dict[str, str] | None" = None
        self._collapsible = False
        self._collapsed: "set[str]" = set()
        self._group_notes: "dict[str, str]" = {}
        self._counted_noun = ""
        self._filter_text = ""
        #: v0.69.0: the filter's own debounce -- see :meth:`sync_filter`.
        #: One timer, reused (stopped and re-armed) rather than one per
        #: keystroke, the same shape `doxa.history.SessionSearch` already
        #: uses for `/search`'s live query. ``_filter_seq`` guards against
        #: a stale re-render the same way that class's own query sequence
        #: does, in case a future caller makes the render itself async;
        #: today's render is synchronous, so stopping the old Timer before
        #: arming a new one is already airtight on its own -- the seq is
        #: defence in depth, not load-bearing yet.
        self._filter_timer: "Any | None" = None
        self._filter_seq = 0
        self._current_id: "str | None" = None
        self._on_select: "Callable[[str], Any] | None" = None
        # v0.67.0: the beliefs/proposals menus' inline row actions -- see
        # RowAction's own docstring. Every OTHER chip menu leaves these at
        # their defaults and nothing below changes for it.
        self._row_prefix_width = 0
        self._row_actions: "list[RowAction] | None" = None
        self._row_action_dispatch: "Callable[[str, str], Any] | None" = None
        self._armed_rid: "str | None" = None
        #: See action_row_action's own docstring -- the debounce guard
        #: against Textual's own double-delivered click.
        self._last_action_signature: "tuple[str, str] | None" = None
        #: True for exactly the two menus this widget does NOT take real
        #: focus for -- see :meth:`open`'s own note on why, and
        #: `doxa.ui.prompt.PromptInput`'s new branch, which is what drives
        #: this widget instead while it is true.
        self._prompt_filter = False
        # v0.69.0: Right/Left expand-in-place -- the beliefs menu's own
        # evidence trail, the one thing the removed beliefs browser had
        # that this widget did not (see EvidenceTrail's old docstring on
        # why an OptionList couldn't hold one before this).
        #
        # A LIST OF ROWS, not one blob: each evidence event a caller's
        # async fetcher returns becomes its OWN extra, disabled,
        # non-selectable row inserted directly under the belief -- the
        # exact fold mechanism `doxa.history.SessionSearch` already uses
        # to insert a header's child rows for `/search`'s Right/Left,
        # reused rather than a second one invented here (a mounted CHILD
        # WIDGET is what the browser did and what an OptionList still
        # cannot do; SEVERAL synthetic rows is what this widget already
        # had the machinery for, via the group-header rows below).
        # ``None`` means this menu carries no expand capability at all
        # (every menu but beliefs -- a staged proposal has no evidence
        # trail to expand).
        self._expand_dispatch: "Callable[[str], Any] | None" = None
        #: See ``open``'s own docstring -- a cheap sync pre-check so a row
        #: KNOWN to have nothing stays silent under Right, no round trip.
        self._expand_available: "Callable[[str], bool] | None" = None
        #: rid -> already-fetched, formatted evidence rows (one list entry
        #: per row this widget will insert). Never an empty list for a rid
        #: that is a KEY of this dict -- the caller's own fetcher returns
        #: at least one row (a "no evidence" line) rather than nothing, so
        #: "fetched and empty" and "not fetched yet" (this rid absent from
        #: both dicts below) stay two different, visibly different states.
        self._expanded: "dict[str, list[str]]" = {}
        #: rids with a fetch in flight -- render a placeholder, not a
        #: crash, if a second Right lands before the first fetch answers
        #: (guarded against in :meth:`expand_current` itself, but kept as
        #: its own set rather than folded into ``_expanded`` so "loading"
        #: and "loaded empty" stay visibly different states).
        self._expanding: "set[str]" = set()

    @property
    def is_open(self) -> bool:
        return bool(self.display)

    @property
    def has_row_actions(self) -> bool:
        return bool(self._row_actions)

    def open(
        self,
        rows: "list[tuple[str, str]]",
        current_id: "str | None",
        on_select: "Callable[[str], Any]",
        *,
        note: str = "",
        title: str = "",
        column_header: "str | None" = None,
        groups: "dict[str, str] | None" = None,
        collapsible: bool = False,
        group_notes: "dict[str, str] | None" = None,
        counted_noun: str = "",
        open_groups: "set[str] | None" = None,
        row_prefix_width: int = 0,
        row_actions: "list[RowAction] | None" = None,
        row_action_dispatch: "Callable[[str, str], Any] | None" = None,
        prompt_filter: bool = False,
        expand_dispatch: "Callable[[str], Any] | None" = None,
        expand_available: "Callable[[str], bool] | None" = None,
    ) -> None:
        """Configure and show. Reopening (a click on a DIFFERENT chip
        while this one is already up -- or the SAME chip re-rendering
        itself with new candidates, see the repo picker's descend-a-
        directory callback) just reconfigures the same instance -- there
        is only ever one picker, so no prior-close bookkeeping is needed
        here.

        ``row_prefix_width``/``row_actions``/``row_action_dispatch``
        (v0.67.0, the beliefs and proposals menus only): each row's label
        is expected to already carry :data:`doxa.ui.labels.
        PICKER_PREFIX_WIDTH` columns of fixed stamp/status/age prefix
        (built by ``_fmt_belief_row``/``_fmt_pending_row``) followed by
        free text -- :meth:`_render_rows` re-trims ONLY the text half at
        that boundary instead of blindly ellipsizing the whole label, and
        appends each :class:`RowAction` as its own fixed-width clickable
        span so the columns line up as a table.

        ``prompt_filter=True`` is the other half of that pair: this widget
        does NOT take real keyboard focus (the ``self.focus()`` call below
        is skipped), so it never competes with the prompt for keystrokes.
        Typing, arrow navigation and the reserved action letters are all
        driven from :class:`doxa.ui.prompt.PromptInput` instead -- see its
        own new branch in ``on_key`` -- through :meth:`sync_filter`,
        :meth:`move_highlight` and :meth:`try_action_key`. Every OTHER
        chip menu (model, branch, effort, mode, sessions, repo) leaves
        this ``False`` and keeps taking real focus exactly as before.

        ``expand_dispatch`` (v0.69.0, the beliefs menu only): an async
        ``rid -> list[str]`` fetcher. When set, Right on the highlighted
        row fetches and inserts each returned string as its OWN extra
        disabled row directly beneath it (see :meth:`expand_current`);
        Left removes them again. The fetcher's contract: return at least
        one row rather than an empty list (a "no evidence" line, not
        nothing) -- an expanded-and-empty belief and a belief that was
        never expanded must look different on screen, not just be
        different internally. ``None`` (every other menu, including the
        proposals one -- a staged proposal carries no evidence trail)
        makes both a no-op.

        ``expand_available`` (v0.69.0): a SYNC, cheap ``rid -> bool``
        pre-check, consulted before ``expand_dispatch`` ever runs. Its
        job is the case ``expand_dispatch`` alone cannot cover cheaply: a
        belief the caller already knows carries no evidence (its own
        ``evidence_count`` says so) must behave EXACTLY like a row with
        no expand capability at all -- Right does nothing, quietly, no
        loading row flashed and then resolved to empty, no round trip
        spent to learn what the row already knew. Omitted (``None``)
        means every row ``expand_dispatch`` covers is assumed available,
        which is correct for every caller that has not opted into this.

        ``column_header`` (v0.69.0): one ALREADY fixed-width string (see
        :func:`doxa.ui.labels.format_picker_column_header`), rendered
        ONCE at the very top -- after ``note`` if there is one, before
        every group header and candidate -- naming what the fixed columns
        are. Reuses the SAME disabled/rid-``""`` convention every other
        inert row in this widget already uses (see :meth:`_render_rows`),
        so it is skipped by cursor movement, unreachable by an action key
        or Enter, was never the row this widget lands the initial
        highlight on, and is not counted by anything that counts
        ``_all_rows`` (the caller's own list never includes it). Hidden
        under a typed filter, the same convention folded GROUP headers
        already follow -- safe to drop rather than merely conventional:
        every row's own columns are fixed-width by construction, so
        losing the header costs a LABEL, never the alignment under it."""
        self._all_rows = list(rows)
        self._current_id = current_id
        self._on_select = on_select
        self._note = note
        self._column_header = column_header
        self._groups = groups
        self._collapsible = bool(collapsible and groups)
        self._group_notes = dict(group_notes or {})
        self._row_prefix_width = row_prefix_width
        self._row_actions = list(row_actions) if row_actions else None
        self._row_action_dispatch = row_action_dispatch
        self._armed_rid = None
        self._last_action_signature = None
        self._prompt_filter = prompt_filter
        self._expand_dispatch = expand_dispatch
        self._expand_available = expand_available
        self._expanded = {}
        self._expanding = set()
        self._counted_noun = counted_noun
        # Every group folded shut on open -- except when the whole list
        # already fits, see AUTOEXPAND_ROWS. The counts in the headers are
        # what makes a fully folded list informative rather than empty:
        # "project (412 beliefs) · user (83 beliefs)" IS the rough answer
        # to what the store holds, and one selection opens the part you
        # came for.
        #
        # ``open_groups`` is the exception that has to exist: a group whose
        # rows are DOORS rather than data must never start folded, or a
        # large store hides the way out of itself behind a fold. No
        # current caller passes one (the beliefs picker's own "open the
        # browser" door row was the original motivating case, removed in
        # v0.69.0 along with the browser it opened) -- kept as a general
        # capability for whichever caller needs it next, not beliefs-
        # specific.
        self._collapsed = set()
        if self._collapsible and len(self._all_rows) > self.AUTOEXPAND_ROWS:
            self._collapsed = set(self._group_labels()) - set(open_groups or ())
        self._filter_text = ""
        if self._filter_timer is not None:
            self._filter_timer.stop()
            self._filter_timer = None
        self.border_title = title
        self._render_rows()
        self.display = True
        if self._prompt_filter:
            # The visibility half of the "prompt becomes a filter" spec: a
            # mode with no on-screen sign is a mode nobody notices they are
            # in. TextArea carries no placeholder (see PromptInput's own
            # docstring on why), so the border title -- already visible,
            # already this widget's own name for what it opened -- is what
            # changes instead.
            with contextlib.suppress(Exception):
                self.pane.query_one("#prompt-input").border_title = (
                    f"filter: {title} — type to search, Esc to close"
                )
        else:
            self.focus()

    def _row_budget(self) -> int:
        """How many columns a row's text may use, right now.

        MEASURED, three tiers, widest correct answer first:

        * ``scrollable_content_region`` -- inside the border, the padding
          AND the scrollbar. The scrollbar is why this is not arithmetic on
          the terminal width: it appears when the list is long, which is
          exactly when rows are longest.
        * ``content_size`` -- inside border and padding, before Textual
          has decided about scrollbars.
        * :data:`doxa.ui.labels.PICKER_ROW_WIDTH` -- for a widget with no
          geometry at all, which is what ``open()`` renders against before
          its first layout."""
        for source in ("scrollable_content_region", "content_size"):
            try:
                width = int(getattr(getattr(self, source), "width", 0) or 0)
            except Exception:  # noqa: BLE001 -- an unlaid-out widget is a tier
                width = 0
            if width > self.ROW_CHROME_COLS:
                return max(20, width - self.ROW_CHROME_COLS)
        return PICKER_ROW_WIDTH

    def _on_resize(self, event: events.Resize) -> None:
        """A row trimmed to yesterday's width lies about what it holds, so
        a resize re-renders the open list -- keeping the highlight."""
        if self.display:
            highlighted = self.highlighted
            self._render_rows()
            if highlighted is not None and highlighted < len(self._rows):
                self.highlighted = highlighted

    def _group_labels(self) -> "list[str]":
        """Every group label present in the current row set, in the order
        the caller's rows put them -- the same walk :meth:`_render_rows`
        does, so the two can never disagree about what a group is."""
        seen: "list[str]" = []
        for rid, _label in self._all_rows:
            group = (self._groups or {}).get(rid, "")
            if group and group not in seen:
                seen.append(group)
        return seen

    def _group_counts(self) -> "dict[str, int]":
        counts: "dict[str, int]" = {}
        for rid, _label in self._all_rows:
            group = (self._groups or {}).get(rid, "")
            if group:
                counts[group] = counts.get(group, 0) + 1
        return counts

    def _header_text(self, group: str, count: int) -> str:
        """One fold header: marker, group, count, and whatever the caller
        wanted said about it.

        The COUNT is this widget's own (it has the rows), the NOTE is the
        caller's -- "9 tested" is a fact about beliefs and a generic
        dropdown has no business knowing it, while "how many rows are in
        this group" is a fact about rows and the caller should not have to
        recount them."""
        marker = "▸" if group in self._collapsed else "▾"
        noun = self._counted_noun
        plural = f" {noun}{'' if count == 1 else 's'}" if noun else ""
        note = self._group_notes.get(group, "")
        inside = f"{count}{plural}" + (f", {note}" if note else "")
        return f"{marker} {group} ({inside})"

    def _action_reserve(self) -> int:
        """Plain-text columns the UNARMED action suffix spends -- reserved
        out of the text column's own budget (see :meth:`_render_rows`) so
        the two never compete for the same terminal cells. The armed
        suffix (one row, transiently) is not budgeted for separately: it
        replaces every other action on that one row rather than adding to
        them, and is close enough in width that a single row occasionally
        running a few cells past this reserve is not the defect a whole
        list silently overflowing its own dropdown would be."""
        if not self._row_actions:
            return 0
        widths = [spec.column_width for spec in self._row_actions]
        return 2 + sum(widths) + 2 * max(0, len(widths) - 1)

    def _action_suffix(self, rid: str) -> str:
        """This row's inline action controls, fixed-width and clickable --
        the v0.67.0 addition. Empty for every menu but beliefs/pending
        (:attr:`_row_actions` is only ever set there) and for the group
        headers/door rows that never reach this call (see
        :meth:`_render_rows`'s own branching).

        ARMED is drawn differently from the rest of :class:`RowAction`'s
        own arming discipline: while THIS row is armed, its other action
        spans disappear -- replaced by the one armed control and a
        disarm hint -- so there is nothing beside the destructive control
        for a stray click to land on.

        PADDING LIVES OUTSIDE THE MARKUP SPAN (v0.69.0 fix). Reported: the
        clickable underline on ``approve``/``retract`` -- the two ``arms``
        verbs -- ran visibly past the word. Measured, not assumed: those
        two are exactly the ones whose ``column_width`` (sized to the
        WIDER of the resting and armed label, so the row does not jump
        width when it arms) exceeded the resting label's own length, and
        the padding that closed that gap used to be ``.ljust``ed INSIDE
        the ``[@click=...][color]...[/][/]`` span -- so the trailing
        spaces were themselves clickable and themselves painted, which is
        what a wider-than-the-word underline actually was. It was also
        the accidental-approve surface item V's own controls have avoided
        since v0.48.0: a click landing in that padding carried ``@click``
        in its style meta same as a click on the word, so a stray click
        BESIDE ``approve`` armed it. Fixed at the source, not only by
        shortening the armed labels below (which now also keeps
        ``column_width`` equal to the resting label's own length for both
        verbs, so this padding is usually empty in practice): the word is
        wrapped in the markup, the padding is plain text appended AFTER
        the closing tags, so the underline, the paint and the CLICK
        TARGET all end exactly where the word does, regardless of how the
        two labels' widths compare."""
        if not self._row_actions:
            return ""
        if rid == self._armed_rid:
            spec = next((s for s in self._row_actions if s.arms), None)
            if spec is not None:
                call = f"row_action({json.dumps(rid)}, {json.dumps(spec.key)})"
                color = spec.armed_color or spec.color
                painted = _escape_markup(spec.armed_label or spec.label)
                return (
                    f"  [@click={call}][{color}]{painted}[/][/]"
                    "  (Esc, or any other row, disarms)"
                )
        parts = []
        for spec in self._row_actions:
            call = f"row_action({json.dumps(rid)}, {json.dumps(spec.key)})"
            label = _escape_markup(spec.label)
            pad = " " * (spec.column_width - len(spec.label))
            parts.append(f"[@click={call}][{spec.color}]{label}[/][/]{pad}")
        return "  " + "  ".join(parts)

    async def _on_click(self, event: events.Click) -> None:
        """A click landing inside a ``[@click=...]`` action span (the
        inline row-action controls :meth:`_action_suffix` paints) is
        brokered against THIS widget's own ``action_*`` methods, the same
        unprefixed ``[@click=...]`` convention every other clickable span
        in this app follows -- rather than OptionList's own click
        handling, which knows only "which
        OPTION was clicked" and would route it into :meth:`select_row`
        instead. Every other click (the row's own text, a group header, a
        different menu entirely) falls through to OptionList's stock
        behaviour, unchanged."""
        meta = event.style.meta or {}
        if "@click" in meta:
            await self.broker_event("click", event)
            return
        await super()._on_click(event)

    def action_row_action(self, rid: str, key: str) -> None:
        """Dispatch one inline row action -- a click on its span (via
        :meth:`_on_click`) or a reserved letter (via :meth:`try_action_key`,
        routed from ``PromptInput``) both land here identically.

        Arming lives HERE, not in the caller's dispatch function: the
        first press on an ``arms`` action re-paints this row and returns
        without calling out at all; only a SECOND press on the SAME row
        applies it. Pressing an arming key on a DIFFERENT row re-arms
        there instead of applying -- "arming any row disarms every other".

        DEBOUNCED against the SAME (rid, key) arriving twice inside one
        refresh cycle -- measured, not assumed: Textual's own mouse-click
        delivery posts a SINGLE physical click's ``OptionSelected``
        TWICE (confirmed against this widget's stock, unmodified
        ``select_row`` too, so it is a Textual/OptionList property, not
        something this method introduced). Harmless for a plain row
        select -- the second delivery bounces off an already-closed
        picker's emptied row list -- but not here: an undebounced second
        delivery would arm AND immediately apply an ``arms`` action on
        ONE physical press, silently defeating the whole reason it arms.
        A genuinely separate second press (the deliberate "arm, then
        press again") is always at least one refresh apart in practice
        and is never the one this suppresses."""
        spec = next((s for s in (self._row_actions or []) if s.key == key), None)
        if spec is None or not rid:
            return
        signature = (rid, key)
        if signature == self._last_action_signature:
            return
        self._last_action_signature = signature
        self.call_after_refresh(self._clear_last_action_signature)
        # _render_rows() ALWAYS resets the highlight to the first
        # selectable row (the normal, correct behaviour after a FILTER
        # keystroke, where the candidate set itself just changed) --
        # wrong here, where arming changes nothing about which rows exist
        # or match. Preserved by INDEX, same pattern _on_resize already
        # uses for the identical reason.
        highlighted = self.highlighted
        if spec.arms and rid != self._armed_rid:
            self._armed_rid = rid
            self._render_rows()
            if highlighted is not None and highlighted < len(self._rows):
                self.highlighted = highlighted
            return
        self._armed_rid = None
        dispatch = self._row_action_dispatch
        self._render_rows()
        if highlighted is not None and highlighted < len(self._rows):
            self.highlighted = highlighted
        if dispatch is not None:
            dispatch(rid, key)

    def _clear_last_action_signature(self) -> None:
        self._last_action_signature = None

    def try_action_key(self, key: str) -> bool:
        """One reserved letter against the CURRENTLY HIGHLIGHTED row --
        ``PromptInput.on_key`` calls this, before it lets an ordinary
        keystroke fall through to the text buffer, only while the filter
        is empty (see that method's own docstring on why the filter wins
        the moment it is not). Returns whether the key was one of this
        menu's own action keys at all, so the caller knows whether to keep
        treating it as ordinary text."""
        if not self._row_actions or self.highlighted is None:
            return False
        if not (0 <= self.highlighted < len(self._rows)):
            return False
        rid, _label = self._rows[self.highlighted]
        if not rid or rid.startswith(self.GROUP_ROW_PREFIX):
            return False
        if not any(s.key == key for s in self._row_actions):
            return False
        self.action_row_action(rid, key)
        return True

    def _highlighted_rid(self) -> "str | None":
        """The candidate the cursor sits on, or ``None`` on a note/header
        row or nothing selectable -- the same guard :meth:`try_action_key`
        applies, factored out because :meth:`expand_current` and
        :meth:`collapse_current` both need it and neither fires an
        action."""
        index = self.highlighted
        if index is None or not (0 <= index < len(self._rows)):
            return None
        rid, _label = self._rows[index]
        if not rid or rid.startswith(self.GROUP_ROW_PREFIX):
            return None
        return rid

    def expand_current(self) -> None:
        """Right: fetch and show the highlighted row's evidence, inserted
        as an extra row directly beneath it -- see :meth:`open`'s own note
        on ``expand_dispatch`` and :meth:`_render_rows`'s own insertion.

        A no-op on every menu but the beliefs one (``_expand_dispatch`` is
        ``None``), on a header/note row (:meth:`_highlighted_rid` already
        excludes those), on a row ``expand_available`` says has nothing (a
        belief whose own ``evidence_count`` is already zero -- no loading
        row flashed only to resolve to "no evidence" a moment later, the
        SAME silent nothing a proposal row already gives Right), and --
        matching ``SessionSearch.expand_current``'s own "no-op on an
        already-open header" contract, the ``/search`` fold gesture this
        reuses rather than inventing a second one -- on a row that is
        already expanded or already fetching.

        NO DOUBLE-CLICK EQUIVALENT, considered and declined (v0.69.0).
        The keyboard gesture above is complete on its own -- this is not a
        missing convenience. A double-click handler here would have to be
        built on top of a widget that ALREADY double-delivers a single
        physical click's ``OptionSelected`` (measured, v0.67.0 --
        :meth:`action_row_action`'s own docstring, and the reason that
        method carries its own same-tick debounce). Distinguishing "one
        click, delivered twice" from "two real clicks" reliably, on a row
        that ALSO carries destructive arm-twice action spans on the same
        line (a double-click landing on `retract`'s cell must not read as
        two presses of it), was judged fiddlier than it is worth: a
        keyboard fold that works beats a mouse gesture that occasionally
        risks retracting a belief. Right/Left cover the requirement in
        full; nothing here reaches for the mouse."""
        rid = self._highlighted_rid()
        if not rid or self._expand_dispatch is None:
            return
        if self._expand_available is not None and not self._expand_available(rid):
            return
        if rid in self._expanded or rid in self._expanding:
            return
        self._expanding.add(rid)
        highlighted = self.highlighted
        self._render_rows()
        if highlighted is not None and highlighted < len(self._rows):
            self.highlighted = highlighted
        self.run_worker(self._fetch_expansion(rid), group="chip-picker-evidence")

    def expand_rows(self, rid: str, rows: "list[str]") -> None:
        """Insert ALREADY-COMPUTED rows beneath ``rid`` -- the same fold
        :meth:`expand_current` produces, for a caller that holds the rows
        rather than an async fetcher to run.

        The beliefs menu's ``g`` graph action (v0.86.0) is the one caller:
        ``lore_core.beliefs.format_edges`` hands back a formatted edge
        block, so there is nothing to fetch lazily and no availability
        pre-check to consult -- the belief either has relations or the
        caller has already turned that into its own "no relations
        recorded" row.

        ONE EXPANSION SLOT PER ROW, shared with the evidence trail. Two
        stacked expansions under one belief would be a wall with no header
        telling a reader which half is which, so the newest one wins:
        ``g`` on a row already showing its evidence replaces it. The
        precedence is not symmetric, and deliberately so -- ``g`` always
        applies, while Right no-ops on a row that is ALREADY expanded (see
        :meth:`expand_current`'s own guard, unchanged), so getting the
        evidence back after a graph takes Left first. That asymmetry
        follows the existing rule rather than carving an exception into
        it: Right has never re-fetched over an open expansion.

        A no-op on a row this menu does not have -- a picker reconfigured
        onto a different row set while the caller was computing must not
        grow an orphan expansion."""
        if not rid or rid not in {r for r, _l in self._all_rows}:
            return
        self._expanding.discard(rid)
        self._expanded[rid] = [str(row) for row in rows] or ["    (nothing to show)"]
        highlighted = self.highlighted
        self._render_rows()
        if highlighted is not None and highlighted < len(self._rows):
            self.highlighted = highlighted

    def collapse_current(self) -> None:
        """Left: fold an expanded row's evidence away again -- a no-op on
        a row that was never expanded, matching ``SessionSearch.
        collapse_current``'s own no-op contract on a leaf with nothing of
        its own left to close."""
        rid = self._highlighted_rid()
        if not rid or rid not in self._expanded:
            return
        del self._expanded[rid]
        highlighted = self.highlighted
        self._render_rows()
        if highlighted is not None and highlighted < len(self._rows):
            self.highlighted = highlighted

    async def _fetch_expansion(self, rid: str) -> None:
        """The worker :meth:`expand_current` starts. Never raises into the
        UI -- a fetch that fails paints the failure as the row's own
        content (as a single-row list) rather than losing the keystroke
        silently, the same "a caveat is never worth raising" posture every
        other picker fetch in this app already takes."""
        dispatch = self._expand_dispatch
        if dispatch is None:
            self._expanding.discard(rid)
            return
        try:
            rows = [str(r) for r in (await dispatch(rid) or [])]
        except Exception as exc:  # noqa: BLE001
            rows = [f"    evidence unavailable — {type(exc).__name__}: {exc}"]
        if not rows:
            # The caller's own contract (see `open`'s docstring) is to
            # return at least a "no evidence" row rather than an empty
            # list -- but a caller that slips is a blank expansion, not a
            # crash: fall back to the SAME "loaded but nothing there"
            # wording rather than trusting an empty list to mean that.
            rows = ["    no evidence rows"]
        self._expanding.discard(rid)
        if not self.display or rid not in {r for r, _l in self._all_rows}:
            # Closed, or reconfigured onto a different row set, while the
            # fetch was in flight -- nothing left to paint this onto.
            return
        self._expanded[rid] = rows
        highlighted = self.highlighted
        self._render_rows()
        if highlighted is not None and highlighted < len(self._rows):
            self.highlighted = highlighted

    @property
    def prompt_filter_active(self) -> bool:
        """Whether ``PromptInput`` should be driving THIS widget right
        now -- open, and opened with ``prompt_filter=True``. The two
        beliefs/proposals menus only; every other chip menu keeps taking
        real focus and this is always False for them."""
        return bool(self.display and self._prompt_filter)

    def move(self, delta: int) -> None:
        """Arrow navigation for the menus this widget does not hold real
        focus for -- OptionList's own up/down BINDINGS never fire here
        (nothing ever focuses this widget while ``prompt_filter`` is
        active), so ``PromptInput.on_key`` calls this instead, reusing
        OptionList's own cursor actions rather than reimplementing
        "skip disabled rows, wrap, keep the highlight visible"."""
        if delta > 0:
            self.action_cursor_down()
        else:
            self.action_cursor_up()

    def sync_filter(self, text: str) -> None:
        """The prompt's current text, mirrored in as this menu's filter --
        ``PromptInput``'s own ``Changed`` handler calls this the same way
        it already syncs ``SlashComplete``/``SessionSearch``. A no-op
        while closed (a stray Changed from some other cause) and when the
        text has not actually moved (avoids re-rendering on every
        keystroke of a DIFFERENT widget's edit -- the two never fire for
        the same reason, but the guard is free and mirrors the shape of
        every other early-return in this class).

        DEBOUNCED (v0.69.0). MEASURED first, not assumed: with 600 rows
        already loaded (the daemon's own belief-picker ceiling), one call
        to :meth:`_render_rows` costs single-digit milliseconds -- an
        in-memory fuzzy-match and re-render, never a query, so the cost
        this debounce hides is NOT the render itself (a debounce that
        only masked slow work would be the wrong fix, and the render here
        is not slow work). What it buys instead is fewer REPAINTS during
        a fast typist's burst -- one composited frame per settled word
        rather than one per keystroke, the same reason `/search` debounces
        even though its own query is often just as fast. Same interval,
        reused rather than re-tuned: :data:`doxa.history.DEBOUNCE_SECS`
        is calibrated to typing cadence (`history.py`'s own comment: long
        enough for one query per word, short enough to feel live), which
        is identical for both surfaces regardless of what either does
        underneath.

        The filter text and the border subtitle update INSTANTLY, every
        keystroke, undebounced -- both are free (a string assignment, no
        row rebuild), and painting the typed text immediately is what
        proves the widget is not hung at the INPUT end while the row list
        catches up a beat later. The trailing "…" is the in-flight
        marker: hide-at-zero, present only while a rebuild is actually
        pending, gone the instant :meth:`_apply_filter` runs -- no
        separate timer or animation for it, it rides the SAME one this
        method already arms."""
        if not self.display or text == self._filter_text:
            return
        self._filter_text = text
        self.border_subtitle = f"/{text} …" if text else ""
        self._filter_seq += 1
        seq = self._filter_seq
        if self._filter_timer is not None:
            self._filter_timer.stop()
        self._filter_timer = self.set_timer(
            DEBOUNCE_SECS, lambda: self._apply_filter(seq)
        )

    def _apply_filter(self, seq: int) -> None:
        """The debounce firing: the actual row rebuild, deferred out of
        :meth:`sync_filter`. Guarded by sequence number (see that
        method's own note on why this is not load-bearing today) and by
        ``display`` -- the picker may have closed while this was pending."""
        self._filter_timer = None
        if seq != self._filter_seq or not self.display:
            return
        self._render_rows()

    def flush_filter(self) -> None:
        """Run a pending debounced filter NOW, skipping the wait --
        public so a caller (and the test suite) can see the settled
        result without sleeping out :data:`doxa.history.DEBOUNCE_SECS`.
        Same reason `SessionSearch.launch` is public for the identical
        case. A no-op when nothing is pending."""
        if self._filter_timer is not None:
            self._filter_timer.stop()
            self._filter_timer = None
            self._render_rows()

    def _render_rows(self) -> None:
        self.clear_options()
        self._rows = []
        if self._note:
            self.add_option(Option(self._note, disabled=True))
            self._rows.append(("", self._note))
        # The column-name header (v0.69.0): ONCE, right after the note,
        # never per group and never a candidate itself -- rid "" is this
        # widget's own established "not a row anything counts" convention
        # (the note above and the plain group header below both already
        # use it). Hidden under a typed filter, matching folded GROUP
        # headers, which drop for the identical reason: once the matcher
        # is doing the ranking a header names nothing a hit doesn't
        # already say better, and every row is fixed-width regardless of
        # whether this one is on screen, so nothing below it drifts.
        if self._column_header and not self._filter_text:
            self.add_option(Option(
                " " * self.ROW_CHROME_COLS + self._column_header, disabled=True,
            ))
            self._rows.append(("", self._column_header))
        candidates = self._all_rows
        grouped = self._groups and not self._filter_text
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
            counts = self._group_counts() if self._collapsible else {}
            budget = self._row_budget()
            current_group = None
            for rid, label in candidates:
                group = self._groups.get(rid, "") if self._groups else ""
                if grouped and not group:
                    # An UNGROUPED row (v0.57.0): rendered where the caller
                    # put it, with no header above it and no fold around
                    # it -- for a row that is a DOOR rather than data (the
                    # beliefs/proposals pickers' own "open the browser"
                    # rows were the original case, removed with the
                    # browser in v0.69.0; no current caller leaves a row
                    # ungrouped, but the mechanism stays general rather
                    # than beliefs-specific). Such a row had to be given a
                    # group of its own purely so the header machinery
                    # would not paint a bare `▎`, which then became a fold
                    # around a single row whose only effect was hiding the
                    # way out.
                    self.add_option(
                        Option(f"   {_escape_markup(ellipsize(label, budget))}")
                    )
                    self._rows.append((rid, label))
                    continue
                if grouped:
                    if group != current_group:
                        current_group = group
                        if self._collapsible:
                            # A SELECTABLE header: folding is the affordance
                            # and a disabled row cannot be selected, so this
                            # one is enabled and select_row intercepts it.
                            head = self._header_text(group, counts.get(group, 0))
                            self.add_option(Option(f" {_escape_markup(head)}"))
                            self._rows.append(
                                (f"{self.GROUP_ROW_PREFIX}{group}", head)
                            )
                        else:
                            self.add_option(Option(f"▎ {group}", disabled=True))
                            self._rows.append(("", f"▎ {group}"))
                    if group in self._collapsed:
                        continue
                if rid == self._current_id:
                    mark = "▸"
                elif rid in self._expanded:
                    # Reuses the SAME single-column mark slot the current-
                    # selection marker above already reserves -- free,
                    # because the beliefs/proposals menus never set
                    # ``current_id`` (there is no "current" belief), so the
                    # column is otherwise always blank for them.
                    mark = "▾"
                elif rid in self._expanding:
                    mark = "…"
                else:
                    mark = " "
                if self._row_prefix_width:
                    # v0.67.0: the beliefs/proposals shape. The stamp/
                    # status/age prefix is ALREADY fixed-width (built by
                    # _fmt_belief_row/_fmt_pending_row) and must never be
                    # cut -- only the free-text tail past it is re-trimmed
                    # here, to min(PICKER_TEXT_CAP, budget) rather than to
                    # budget alone, matching the operator's literal spec --
                    # MINUS whatever the row's own action controls reserve
                    # (see _action_reserve), so text and actions never
                    # compete for the same terminal cells and a row with
                    # actions degrades the same way a plain one already
                    # did rather than overflowing its own dropdown. The
                    # matcher still scored the WHOLE untrimmed label
                    # above, prefix and all.
                    prefix = label[: self._row_prefix_width]
                    rest = label[self._row_prefix_width:]
                    reserve = self._action_reserve()
                    room = max(0, budget - self._row_prefix_width)
                    # Room for the actions is checked before they are
                    # drawn -- on a terminal too narrow for the prefix,
                    # the text AND the actions (the prefix alone can run
                    # past half of an 80-column terminal), the actions
                    # drop rather than the row overflowing its own
                    # dropdown. The row is still reachable then, just
                    # through the per-row action sub-menu (select it
                    # outright) instead of inline.
                    fits_actions = reserve and reserve <= room
                    text_cap = min(PICKER_TEXT_CAP, room) - (reserve if fits_actions else 0)
                    shown = prefix + ellipsize(rest, max(0, text_cap))
                    suffix = self._action_suffix(rid) if fits_actions else ""
                else:
                    # Trimmed HERE, against the measured width, rather than
                    # by the formatter against a constant -- which also
                    # means the matcher above scored the WHOLE row, so a
                    # word past the visible cut stays findable.
                    shown = ellipsize(label, budget)
                    suffix = ""
                option_text = f" {mark} {_escape_markup(shown)}" + suffix
                self.add_option(Option(option_text))
                self._rows.append((rid, label))
                # The evidence trail, ALREADY fetched -- inserted as
                # SEVERAL extra, disabled, unselectable rows directly
                # under the belief they belong to, one per evidence
                # event, exactly where the removed browser mounted its
                # own single ``EvidenceTrail`` widget. Empty ``rid`` is
                # this widget's own established convention for "not a
                # candidate" (the note row and the plain, non-collapsible
                # group header above both use it too), so every existing
                # rid-based skip (select_row, try_action_key, the "first
                # selectable row" scan below) already treats every one of
                # these rows correctly with no changes -- and because
                # OptionList's OWN cursor movement already skips disabled
                # options (``find_next_enabled``, this class's own docstring),
                # the highlight cannot land on one: an action key always
                # acts on the belief that owns the evidence, never on the
                # evidence itself.
                #
                # SUPPRESSED while a filter is typed, the same rule
                # folded groups already follow just above (`grouped =
                # self._groups and not self._filter_text`): a filter is
                # scoring `_all_rows`' labels, which never included
                # evidence text, so a filtered view showing rows a typed
                # word cannot explain would be confusing on top of being
                # unfindable. The fetched trail itself is NOT forgotten --
                # `_expanded` is untouched here -- so clearing the filter
                # restores exactly the expansion state that was there
                # before, the same round-trip a folded group already
                # makes.
                if not self._filter_text:
                    for evidence_row in self._expanded.get(rid, ()):
                        self.add_option(Option(evidence_row, disabled=True))
                        self._rows.append(("", ""))
                    if rid in self._expanding:
                        self.add_option(Option("      … loading", disabled=True))
                        self._rows.append(("", ""))
        first = next((i for i, (rid, _l) in enumerate(self._rows) if rid), None)
        self.highlighted = first
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

    def toggle_group(self, group: str) -> None:
        """Fold one group open or shut, keeping the highlight on its own
        header so a second Enter folds it back -- a fold you have to hunt
        for the header again to undo is a fold nobody uses twice."""
        if group in self._collapsed:
            self._collapsed.discard(group)
        else:
            self._collapsed.add(group)
        self._render_rows()
        target = f"{self.GROUP_ROW_PREFIX}{group}"
        for index, (rid, _label) in enumerate(self._rows):
            if rid == target:
                self.highlighted = index
                break

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
        if rid.startswith(self.GROUP_ROW_PREFIX):
            # This widget's own affordance: fold, stay open, and never let
            # a header reach the caller's callback as if it were a row.
            self.toggle_group(rid[len(self.GROUP_ROW_PREFIX):])
            return
        callback = self._on_select
        self.close()
        self.pane.query_one("#prompt-input").focus()
        if callback is not None:
            callback(rid)

    def close(self) -> None:
        was_prompt_filter = self._prompt_filter
        if self.display:
            self.display = False
        self._on_select = None
        self._all_rows = []
        self._rows = []
        self._collapsed = set()
        self._group_notes = {}
        self._collapsible = False
        self._counted_noun = ""
        self._filter_text = ""
        if self._filter_timer is not None:
            self._filter_timer.stop()
            self._filter_timer = None
        self._row_prefix_width = 0
        self._row_actions = None
        self._row_action_dispatch = None
        self._armed_rid = None
        self._last_action_signature = None
        self._prompt_filter = False
        self._expand_dispatch = None
        self._expand_available = None
        self._expanded = {}
        self._expanding = set()
        self.border_title = ""
        self.border_subtitle = ""
        if was_prompt_filter:
            # The filter does not survive the close (item 2's own spec) --
            # and the border-title mode indicator goes with it. The prompt
            # never lost real focus in this mode (see open()'s own note),
            # so there is nothing to focus back -- only content to clear.
            with contextlib.suppress(Exception):
                prompt = self.pane.query_one("#prompt-input")
                prompt.border_title = ""
                prompt.clear()


class CloseWithTurnRunning(ModalScreen[str]):
    """**Ctrl+Q** with a turn still running: terminate, detach, or neither.

    The three-way choice is the point. Silently killing a running turn
    throws away work the user is waiting for; silently keeping it alive is
    the leak this whole change exists to end. So the one case where both
    defaults are wrong asks, and every other close stays instant.

    v0.28.0 -- the same two fixes CompactConfirm below carries, because
    this dialog had the SAME two defects and only the twin was reported:
    its button row was styled `height: 1; padding-top: 1`, which under
    Textual's border-box model renders every button at zero height (see
    theme.tcss's own comment on #close-confirm-buttons), and Enter did
    nothing. Enter now takes the action the keystroke that OPENED this
    dialog already asked for -- and *that* rule is what this class got
    wrong for six releases.

    **The default is TERMINATE.** Reported twice from live use: *"CTRL+Q
    is still just sending a session to background and detaches it instead
    of finalising the session"* -- the operator's words are, almost
    verbatim, the toast ``DoxaApp._close_pane`` prints on the DETACH
    branch ("detached -- still running in the background"), which is the
    tell: they were reaching this dialog and taking the door it labelled
    as the default. Through v1.6.0 that door was ``detach``, on the
    reasoning quoted in this docstring's own history: *Ctrl+W means "close
    this tab", and the non-destructive reading of that is DETACH.* True
    when it was written -- and stale by v0.58.0, when ``action_close_tab``
    stopped asking anything and went straight to ``_close_pane(terminate=
    False)``. Ctrl+Q has been this dialog's ONLY caller ever since (see
    ``DoxaApp._end_session``, and tests/test_subagent_tracker.py, which
    asserts Ctrl+W never opens it), and Ctrl+Q does not mean "close this
    tab" -- it means *end this session, finalize now*. A confirm whose
    Enter key does the opposite of the keystroke that opened it is not a
    safety net, it is a trap: the operator pressed the key documented as
    "End this session (finalize now)", pressed the labelled default, and
    got a session left running in the background.

    So Enter (and ``t``) terminate, ``d`` is the deliberate detach, and
    Escape still cancels -- the destructive answer is the one the gesture
    asked for, and the un-asked-for one now costs a letter. Nothing about
    ASKING changes: a turn in flight is still never killed without this
    dialog. Every label states its own key, so the dialog says what to
    press instead of leaving it to be guessed."""

    BINDINGS = [("escape", "pick_cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="close-confirm"):
            yield Static("▎ a turn is still running", id="close-confirm-title")
            yield Static(
                "terminate  — end the session now, losing the running turn\n"
                "detach     — close this tab and leave the turn running\n"
                "cancel     — keep the tab open",
                id="close-confirm-body",
            )
            with Horizontal(id="close-confirm-buttons"):
                yield Static("[ terminate · enter ]", id="close-terminate")
                yield Static("[ detach · d ]", id="close-detach")
                yield Static("[ cancel · esc ]", id="close-cancel")

    def action_pick_cancel(self) -> None:
        self.dismiss("cancel")

    def on_key(self, event: events.Key) -> None:
        choice = {
            "t": "terminate",
            "d": "detach",
            # Enter = the default door, the one the buttons label as such,
            # and the one Ctrl+Q already asked for. "return" is listed
            # alongside "enter" only for terminals whose key name Textual
            # has not normalized; both mean one keycap.
            "enter": "terminate",
            "return": "terminate",
            "c": "cancel",
        }.get(event.key)
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


class CompactConfirm(ModalScreen[bool]):
    """The ctx% chip's click target used to fire ``/compact`` on a single
    click, no warning -- reported, and a real defect: compaction is lossy
    and there is no undo, so a misclick silently throws conversation detail
    away. This is the confirm that closes it.

    House precedent for the SHAPE: CloseWithTurnRunning above, not
    NeedsInputPopup. NeedsInputPopup is PROMPT-driven (``can_focus =
    False``, answered through PromptInput's own key protocol because
    typing owns focus at that point) for a reason that does not apply here
    -- it exists to answer an ask_user/permission request the ENGINE is
    genuinely waiting on. A compact confirm has nothing on the other end
    waiting; it is a plain UI yes/no, exactly CloseWithTurnRunning's own
    shape (a focused ``ModalScreen``, pushed with ``push_screen_wait`` from
    a worker, Esc/a letter key/a click all dismiss it) -- just two doors
    instead of three.

    The body states what is actually at stake -- the CURRENT context
    percentage, and that compacting summarizes and discards the earlier
    detail -- not a bare "are you sure?": the whole point of asking is to
    say what the click is about to do.

    v0.28.0 -- reported: "clicking on ctx chip show a modal message, but no
    button to continue, no OK, enter does nothing". Two defects in one
    sentence. (1) `#compact-confirm-buttons` was `height: 1; padding-top:
    1`; Textual's box model is border-box, so the padding consumed the
    whole declared row and both Statics laid out at Size(width=0,
    height=0) -- the dialog genuinely had no visible doors (theme.tcss
    carries the fix and its reasoning). (2) Only Esc and y/c/n were bound;
    Enter -- the key anyone presses at a confirm -- was dead. Enter now
    takes the action the CLICK that opened this dialog already asked for
    (the operator clicked "compact"), Esc still cancels, and both button
    labels now name their own key so the dialog is self-describing."""

    BINDINGS = [("escape", "pick_cancel", "Cancel")]

    def __init__(self, percentage: "float | None") -> None:
        super().__init__()
        self._percentage = percentage

    def compose(self) -> ComposeResult:
        pct_text = (
            f"{self._percentage:.0f}%" if self._percentage is not None
            else "an unknown amount"
        )
        with Vertical(id="compact-confirm"):
            yield Static("▎ compact this session's context?", id="compact-confirm-title")
            yield Static(
                f"context is {pct_text} full. compacting summarizes the "
                "conversation so far and DISCARDS the earlier detail -- "
                "there is no undo.",
                id="compact-confirm-body",
            )
            with Horizontal(id="compact-confirm-buttons"):
                yield Static("[ compact · enter ]", id="compact-confirm-yes")
                yield Static("[ cancel · esc ]", id="compact-confirm-no")

    def action_pick_cancel(self) -> None:
        self.dismiss(False)

    def on_key(self, event: events.Key) -> None:
        # "return" rides alongside "enter" only for terminals whose key name
        # Textual has not normalized; both mean the one keycap the button
        # label now names.
        choice = {
            "y": True, "enter": True, "return": True,
            "c": False, "n": False,
        }.get(event.key)
        if choice is not None:
            event.stop()
            self.dismiss(choice)

    @on(events.Click, "#compact-confirm-yes")
    def _click_yes(self, event: events.Click) -> None:
        event.stop()
        self.dismiss(True)

    @on(events.Click, "#compact-confirm-no")
    def _click_no(self, event: events.Click) -> None:
        event.stop()
        self.dismiss(False)


class PermissionModeConfirm(ModalScreen[bool]):
    """``/mode bypassPermissions`` (and ``auto``, and ``dontAsk``): the one
    door to a mode where DOXA stops asking you about a tool call.

    The SHAPE is :class:`CompactConfirm`'s above, and the reasoning
    transfers exactly. That dialog exists because compaction was lossy,
    un-undoable and one unconfirmed click away; these three modes are the
    same asymmetry moved from the transcript to the filesystem. The body
    therefore states WHAT STOPS HAPPENING -- in the second person, naming
    the approval gate the user actually experiences -- rather than asking
    "are you sure?", a question nobody has ever answered with information.

    The three modes are not interchangeable and the body does not pretend
    they are: ``bypassPermissions`` runs everything unapproved, ``auto``
    hands the decision to a model classifier, ``dontAsk`` fails the calls
    instead of asking. One dialog, three bodies, because a generic "this
    changes permissions" line would be equally useful for all three, which
    is to say not at all.

    Esc cancels. Unlike CompactConfirm, **Enter is NOT the accepting
    door**: there, Enter completes an action the user's own click already
    asked for; here, the dialog is the last thing between a keystroke and
    an unattended agent. The accepting key is a letter the user has to
    mean -- ``y`` -- and both doors name their own key, the house
    convention since v0.28.0."""

    BINDINGS = [("escape", "pick_cancel", "Cancel")]

    def __init__(self, mode: str, current: str) -> None:
        super().__init__()
        self._mode = mode
        self._current = current

    def compose(self) -> ComposeResult:
        from .labels import MODE_EXPLAIN

        what = MODE_EXPLAIN.get(self._mode, "this mode changes who approves tool calls")
        with Vertical(id="mode-confirm"):
            yield Static(
                f"▎ switch this session to {self._mode}?",
                id="mode-confirm-title",
            )
            yield Static(
                f"{what}.\n\n"
                f"this session is on {self._current} now. after the switch, "
                "DOXA's permission dialog stops appearing for the calls that "
                "mode covers -- there is no prompt left to decline, because "
                "nothing will ask.\n\n"
                "it applies to THIS session only and is never written to "
                "your settings.",
                id="mode-confirm-body",
            )
            with Horizontal(id="mode-confirm-buttons"):
                yield Static(f"[ switch to {self._mode} · y ]", id="mode-confirm-yes")
                yield Static("[ cancel · esc ]", id="mode-confirm-no")

    def action_pick_cancel(self) -> None:
        self.dismiss(False)

    def on_key(self, event: events.Key) -> None:
        # Enter is DELIBERATELY absent from the accepting side (see the
        # class docstring) and deliberately present on the cancelling one:
        # the reflex key at a dialog must not be the one that disarms the
        # approval gate, and a user who hits it and gets "nothing changed"
        # has lost nothing at all.
        choice = {
            "y": True,
            "n": False, "c": False, "enter": False, "return": False,
        }.get(event.key)
        if choice is not None:
            event.stop()
            self.dismiss(choice)

    @on(events.Click, "#mode-confirm-yes")
    def _click_yes(self, event: events.Click) -> None:
        event.stop()
        self.dismiss(True)

    @on(events.Click, "#mode-confirm-no")
    def _click_no(self, event: events.Click) -> None:
        event.stop()
        self.dismiss(False)


class ResumeConfirm(ModalScreen[bool]):
    """Enter on a ``/search`` session header (v0.56.0): reopen this
    conversation?

    THE SHAPE is :class:`CompactConfirm`'s, above, and deliberately not a
    fourth invention -- a focused ``ModalScreen``, a title row, a body, a
    row of Statics that each name their own key, Esc cancels, Enter takes
    the affirmative. It belongs to that family for the same reason
    CompactConfirm belongs to CloseWithTurnRunning's: nothing is waiting
    on the other end. A resume is a plain UI yes/no, not an engine request
    the way NeedsInputPopup's prompt-driven answer is.

    THE BODY STATES WHAT WILL HAPPEN and does not ask "are you sure?" --
    the house rule, and here it has real work to do, because a resume is
    several non-obvious things at once: it opens a NEW TAB (the current
    pane's own session keeps running, untouched), the model comes back
    with the conversation's history in context, and the prior turns are
    re-rendered from the transcript on disk so the two agree about what
    was said. Every one of those is a thing a user would otherwise have to
    discover.

    It also states what will NOT happen. ``reason`` carries
    :func:`doxa.history.resume_state`'s explanation for a conversation
    that cannot be resumed -- still running, cwd gone, or (the common one
    for a while yet) started before DOXA and the CLI shared a session id.
    Then this dialog has ONE door, it says why, and there is nothing to
    confirm. Showing the refusal here rather than failing a turn later is
    the entire point of checking before offering.

    v0.28.0's defect is why ``#resume-confirm-buttons`` is ``height:
    auto`` in theme.tcss and why this class ships with a test asserting
    real rendered geometry: ``height: 1; padding-top: 1`` under Textual's
    border-box model draws buttons at ZERO height -- present in the DOM,
    passing every ``query_one``, drawn nowhere -- and that shipped for a
    full release because the tests asserted the modal was pushed, never
    that anything was visible."""

    BINDINGS = [("escape", "pick_cancel", "Cancel")]

    def __init__(
        self,
        title: str,
        session_id: str,
        *,
        when: str = "",
        cwd: str = "",
        reason: str = "",
    ) -> None:
        super().__init__()
        self._title = (title or "").strip() or session_id[:8] or "this session"
        self._session_id = session_id
        self._when = when
        self._cwd = cwd
        # Empty means resumable. Non-empty is the refusal, in the words
        # doxa.history.resume_state already chose -- this dialog does not
        # re-word somebody else's finding.
        self.reason = reason

    @property
    def resumable(self) -> bool:
        return not self.reason

    def body_text(self) -> str:
        """The body, built once here so the test that asserts it is on
        screen and the compose that puts it there read the same string."""
        where = f"\ncwd      {self._cwd}" if self._cwd else ""
        when = f"  ·  last active {self._when}" if self._when else ""
        head = f"session  {self._session_id}{where}{when}"
        if not self.resumable:
            return f"{head}\n\n{self.reason}"
        return (
            f"{head}\n\n"
            "resuming opens this conversation in a NEW TAB. the model "
            "comes back with its history in context, and the turns so far "
            "are re-rendered from the transcript on disk so you can read "
            "what it remembers. this tab's own session keeps running, "
            "untouched."
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="resume-confirm"):
            yield Static(
                f"▎ resume “{self._title}”?" if self.resumable
                else f"▎ cannot resume “{self._title}”",
                id="resume-confirm-title",
            )
            yield Static(self.body_text(), id="resume-confirm-body")
            with Horizontal(id="resume-confirm-buttons"):
                if self.resumable:
                    yield Static("[ resume · enter ]", id="resume-confirm-yes")
                    yield Static("[ cancel · esc ]", id="resume-confirm-no")
                else:
                    yield Static("[ close · enter ]", id="resume-confirm-no")

    def action_pick_cancel(self) -> None:
        self.dismiss(False)

    def on_key(self, event: events.Key) -> None:
        # "return" rides alongside "enter" for terminals whose key name
        # Textual has not normalized -- same pair CompactConfirm binds.
        # On a REFUSAL every key that could mean yes means close instead:
        # there is no affirmative to take, and a dialog whose only door is
        # labelled "close" must not have a second, invisible one.
        choice = {
            "y": True, "r": True, "enter": True, "return": True,
            "n": False, "c": False,
        }.get(event.key)
        if choice is None:
            return
        event.stop()
        self.dismiss(choice and self.resumable)

    @on(events.Click, "#resume-confirm-yes")
    def _click_yes(self, event: events.Click) -> None:
        event.stop()
        self.dismiss(True)

    @on(events.Click, "#resume-confirm-no")
    def _click_no(self, event: events.Click) -> None:
        event.stop()
        self.dismiss(False)


class AboutDialog(ModalScreen[None]):
    """``/about`` (item Z): what DOXA this is, and what a bug report needs.

    The SAME shape as :class:`CompactConfirm` and
    :class:`CloseWithTurnRunning` above -- a focused ``ModalScreen``, a
    title bar, a body, a row of Statics that each name their own key, Esc
    closes -- because a third modal inventing a fourth shape is how a TUI
    stops feeling like one program.

    The rows are :func:`doxa.version.about_rows`, measured when the dialog
    is constructed; the copy door puts :func:`doxa.version.about_text` --
    the same string, from the same builder -- on the clipboard, so what
    lands in an issue is what the user was reading rather than a
    re-derivation of it.

    v0.28.0's defect is why ``#about-buttons`` is ``height: auto`` in
    theme.tcss and why this class ships with a test asserting real
    rendered geometry: ``height: 1; padding-top: 1`` under Textual's
    border-box model draws buttons at ZERO height; they pass every
    ``query_one()`` a suite can write, and that shipped for a full release
    because the tests asserted the modal was pushed, never that anything
    was visible."""

    BINDINGS = [("escape", "pick_close", "Close")]

    def __init__(self, update_available: "bool | None" = None) -> None:
        super().__init__()
        # Built ONCE, here: the body and the copy door must be the same
        # string, and re-deriving it per compose would let a `git status`
        # landing between the two make them disagree.
        self.text = version_mod.about_text(update_available)

    def compose(self) -> ComposeResult:
        with Vertical(id="about"):
            yield Static("▎ about DOXA", id="about-title")
            yield Static(self.text, id="about-body")
            with Horizontal(id="about-buttons"):
                yield Static("[ copy · c ]", id="about-copy")
                yield Static("[ close · esc ]", id="about-close")

    def action_pick_close(self) -> None:
        self.dismiss(None)

    def _copy(self) -> None:
        """Clipboard through the app's own OSC-52 door -- the SAME
        ``copy_to_clipboard`` the sessions picker's copy row uses, so this
        app has one clipboard path rather than two."""
        with contextlib.suppress(Exception):
            self.app.copy_to_clipboard(self.text)
            self.notify("about: copied")

    def on_key(self, event: events.Key) -> None:
        if event.key == "c":
            event.stop()
            self._copy()
            return
        # Esc is the binding; Enter and q are the two other keys anyone
        # actually presses at a read-only panel, and a panel with nothing
        # to confirm has no reason to treat them differently.
        if event.key in ("enter", "return", "q"):
            event.stop()
            self.dismiss(None)

    @on(events.Click, "#about-copy")
    def _click_copy(self, event: events.Click) -> None:
        event.stop()
        self._copy()

    @on(events.Click, "#about-close")
    def _click_close(self, event: events.Click) -> None:
        event.stop()
        self.dismiss(None)


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

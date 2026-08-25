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
from .labels import _escape_markup


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
        self._groups: "dict[str, str] | None" = None
        self._collapsible = False
        self._collapsed: "set[str]" = set()
        self._group_notes: "dict[str, str]" = {}
        self._counted_noun = ""
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
        groups: "dict[str, str] | None" = None,
        collapsible: bool = False,
        group_notes: "dict[str, str] | None" = None,
        counted_noun: str = "",
        open_groups: "set[str] | None" = None,
    ) -> None:
        """Configure and show. Reopening (a click on a DIFFERENT chip
        while this one is already up -- or the SAME chip re-rendering
        itself with new candidates, see the repo picker's descend-a-
        directory callback) just reconfigures the same instance -- there
        is only ever one picker, so no prior-close bookkeeping is needed
        here."""
        self._all_rows = list(rows)
        self._current_id = current_id
        self._on_select = on_select
        self._note = note
        self._groups = groups
        self._collapsible = bool(collapsible and groups)
        self._group_notes = dict(group_notes or {})
        self._counted_noun = counted_noun
        # Every group folded shut on open -- except when the whole list
        # already fits, see AUTOEXPAND_ROWS. The counts in the headers are
        # what makes a fully folded list informative rather than empty:
        # "project (412 beliefs) · user (83 beliefs)" IS the rough answer
        # to what the store holds, and one selection opens the part you
        # came for.
        #
        # ``open_groups`` is the exception that has to exist: a group whose
        # rows are DOORS rather than data (the beliefs picker's "open the
        # browser" row lives in one) must never start folded, or a large
        # store hides the way out of itself behind a fold.
        self._collapsed = set()
        if self._collapsible and len(self._all_rows) > self.AUTOEXPAND_ROWS:
            self._collapsed = set(self._group_labels()) - set(open_groups or ())
        self._filter_text = ""
        self.border_title = title
        self._render_rows()
        self.display = True
        self.focus()

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

    def _render_rows(self) -> None:
        self.clear_options()
        self._rows = []
        if self._note:
            self.add_option(Option(self._note, disabled=True))
            self._rows.append(("", self._note))
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
            current_group = None
            for rid, label in candidates:
                if grouped:
                    group = self._groups.get(rid, "")
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
                mark = "▸" if rid == self._current_id else " "
                self.add_option(Option(f" {mark} {_escape_markup(label)}"))
                self._rows.append((rid, label))
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
        self.border_title = ""
        self.border_subtitle = ""


class CloseWithTurnRunning(ModalScreen[str]):
    """Ctrl+W with a turn still running: terminate, detach, or neither.

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
    dialog already asked for -- Ctrl+W means "close this tab", and the
    non-destructive reading of that is DETACH (the tab closes, the turn
    survives, and `/sessions` can re-attach it); terminate stays a
    deliberate `t`, never a default. Every label states its own key, so
    the dialog says what to press instead of leaving it to be guessed."""

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
                yield Static("[ terminate · t ]", id="close-terminate")
                yield Static("[ detach · enter ]", id="close-detach")
                yield Static("[ cancel · esc ]", id="close-cancel")

    def action_pick_cancel(self) -> None:
        self.dismiss("cancel")

    def on_key(self, event: events.Key) -> None:
        choice = {
            "t": "terminate",
            "d": "detach",
            # Enter = the default door, the one the buttons label as such.
            # "return" is listed alongside "enter" only for terminals whose
            # key name Textual has not normalized; both mean one keycap.
            "enter": "detach",
            "return": "detach",
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

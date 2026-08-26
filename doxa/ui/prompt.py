# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.ui.prompt -- the prompt input and its key routing.

Extracted from ``doxa/app.py`` unchanged. One widget, but its own module:
:class:`PromptInput` is the single arbiter of what a keystroke means with
three popups potentially open above it, and that priority order is the
whole reason the class is 366 lines. Folding it into
:mod:`doxa.ui.dialogs` would file the arbiter under one of the things it
arbitrates between.
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea

from .. import history as history_mod
from .. import paste as paste_mod
from ..history import SessionSearch  # noqa: F401 -- annotation-only
from .dialogs import (  # noqa: F401 -- NeedsInputPopup is annotation-only
    _NEEDS_INPUT_DIGIT_KEYS,
    NeedsInputPopup,
    SlashComplete,
)


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
    given terminal actually distinguishes from bare Enter. Both are bound
    here regardless, so neither terminal family is left without a
    deliberate-newline key; item O's keyboard-protocol detection
    (:mod:`doxa.keyboard`) is what now TELLS the operator which of the two
    they have, on ``/about`` and in ``/doctor``. It does not change this
    pair, and deliberately: reporting what a terminal can do is a
    different job from re-mapping keys around it, and Alt+Enter working
    everywhere is why there was never anything here to re-map.

    The needs-input dialog (queue item 5) is checked FIRST, ahead of
    search and the slash dropdown: a pending AskUserQuestion/permission
    request represents something ELSE actually waiting on you, not a UI
    convenience you opened yourself, so it wins any (in practice
    vanishingly rare) contention for the same keystroke. The search popup
    is checked next because it is the one that can be open while a
    command name is fully typed (``/search ...``); the two ordinary
    popups are mutually exclusive in practice, and this settles the order
    anyway.

    v0.56.0 changed exactly one meaning in that protocol: Enter on a
    ``/search`` SESSION HEADER posts :class:`ResumeRequested` instead of
    toggling the header's fold. The old comment on that branch reasoned
    that toggling was "the ONLY thing Enter can mean here", which held
    only while a header had nothing behind it worth activating. Right and
    Left already expand and collapse (item I bound both), so the fold lost
    nothing and Enter came to mean here what it means everywhere else in
    this app. Enter on a HIT row is untouched -- ``take_hit`` still
    replaces the query line with the chosen excerpt."""

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

    class ResumeRequested(Message):
        """Enter (or a click) on a ``/search`` SESSION HEADER row: the
        user wants that conversation back (v0.56.0).

        Carries the header's whole group dict -- session id, title, cwd,
        timestamp -- because everything downstream needs all four: the
        confirm modal shows title/when/cwd, the eligibility check needs
        id and cwd, and the resumed tab is born labelled from the title.
        Passing the id alone would mean re-querying LORE for facts the row
        was already holding.

        Posted, not handled here, for the same reason NeedsInputChoice is:
        answering a modal means ``push_screen_wait`` from a worker, and
        opening a tab is the app's job, not a text widget's."""

        def __init__(self, group: dict) -> None:
            self.group = dict(group)
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
        chip_picker: Any,
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
        # v0.67.0: the beliefs/proposals chip menus' "prompt becomes a
        # filter" mode -- a FOURTH prompt-driven popup, in spirit, though
        # ChipPicker itself is not can_focus=False like the other three
        # (every OTHER chip menu still takes real focus; only these two
        # ever set `prompt_filter=True`, checked live via
        # `chip_picker.prompt_filter_active` below rather than a
        # constructor-time distinction, since one widget instance serves
        # every chip menu). Typed here for the same reason the other
        # three are: `doxa.ui.dialogs.ChipPicker` importing this module
        # back for the annotation would be circular.
        self.chip_picker: Any = chip_picker
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
        if self.chip_picker.prompt_filter_active:
            # v0.67.0: the beliefs/proposals chip menus. Checked ahead of
            # `search`/`dropdown` (mutually exclusive with both anyway --
            # opening a ChipPicker closes them, see
            # `PaneChipsMixin._open_chip_picker`) and BEHIND needs-input,
            # for the same reason the other three defer to it: a pending
            # AskUserQuestion/permission request is something ELSE waiting
            # on you, not a UI surface you opened yourself -- it can, in
            # principle, arrive WHILE a chip menu happens to be open, even
            # though opening one is itself guarded against an ALREADY
            # pending question (see `_open_chip_picker`).
            #
            # THE COLLISION RULE, stated once, here: the reserved letters
            # (`a`/`r` for proposals, `y`/`c`/`s`/`r` for beliefs) act on
            # the highlighted row ONLY while the filter is EMPTY. The
            # moment any filter text exists, every key -- including those
            # five -- is ordinary text, synced to the filter like anything
            # else (`_on_prompt_changed`, this pane's Changed handler).
            # This is a real, stated trade-off: searching for a claim
            # that happens to START with one of those five letters costs
            # one throwaway keystroke first. The alternative -- letting a
            # bare letter act UNCONDITIONALLY -- is the one this rule
            # exists to rule out: typing "stale" into the filter must
            # never retract, confirm or approve anything on the way
            # through, and gating on an empty filter is what makes that
            # true by construction rather than by care at each call site.
            picker = self.chip_picker
            handled = True
            if event.key == "escape":
                picker.close()
            elif event.key == "down":
                picker.move(1)
            elif event.key == "up":
                picker.move(-1)
            elif event.key == "enter":
                if picker.highlighted is not None:
                    picker.select_row(picker.highlighted)
            elif event.key == "right" and self.text == "":
                # v0.69.0: the evidence trail the removed beliefs browser
                # carried, now expanded in place on the highlighted row --
                # same gesture `/search`'s own result list already uses to
                # open a fold (`self.search.expand_current()` below), and
                # gated on an empty filter for the identical collision
                # reason the reserved letters two lines down are: with
                # text already typed, Right is cursor movement inside it,
                # not a row command. A no-op on every OTHER chip menu
                # (ChipPicker.expand_current itself declines when the menu
                # carries no `expand_dispatch`), so this line changes
                # nothing for them.
                picker.expand_current()
            elif event.key == "left" and self.text == "":
                picker.collapse_current()
            elif (
                self.text == "" and event.is_printable and event.character
                and picker.try_action_key(event.character)
            ):
                pass  # consumed as a row action -- never reaches the buffer
            else:
                handled = False
            if handled:
                event.stop()
                event.prevent_default()
                return
            # Anything else -- a printable character, backspace, a cursor
            # key this branch does not name -- falls through to ordinary
            # TextArea editing below, exactly like the three popups
            # beneath this one. `_on_prompt_changed` syncs the RESULT into
            # the picker's filter a moment later; Enter is never reached
            # from here without the `event.key == "enter"` arm above
            # having already returned, so a filter string can never be
            # submitted as a turn.
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
                group = self.search.chosen_session()
                if group is not None:
                    # v0.56.0 -- Enter on a session header REPURPOSED.
                    #
                    # It used to toggle the fold, on the reasoning that "a
                    # header row is never itself an excerpt, so this is
                    # the ONLY thing Enter can mean here". That was true
                    # of the meanings available then. It is not true now:
                    # a header names a CONVERSATION, and a conversation is
                    # something you can reopen.
                    #
                    # Nothing is lost. Right already expands a fold and
                    # Left already collapses it (item I bound both), so
                    # the toggle keeps two keys of its own and Enter is
                    # free to mean the thing Enter means everywhere else
                    # in this app -- activate the highlighted row. On a
                    # HIT row that still means take_hit(), unchanged and
                    # deliberately so: the excerpt path is what most
                    # /search traffic is.
                    #
                    # The pane runs it (a modal answer must be awaited
                    # from a worker, and opening a tab is app-level work);
                    # this widget only knows which row the caret is on.
                    self.post_message(self.ResumeRequested(group))
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

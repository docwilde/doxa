# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.appwindow.panetree -- the tree of panes, and the keyboard inside it.

The fifth of DoxaApp's method families to become a mixin, on the terms the
first four set: one family per module, same class, no behaviour change.
This one is the SHAPE of the window rather than anything running in it --
which groups, tabs, panes and surfaces are mounted, which single one of
them holds the keyboard, how a pane divides, and how much room each half
gets. Every other family asks this one where things are.

The module is ``panetree`` and not ``layout`` because :mod:`doxa.layout`
is taken. That module is the geometry itself -- the split floor, the
divider arithmetic, the neighbour lookup -- and this one is its caller,
importing it here as ``layout_mod`` exactly as doxa/app.py did. Two names
one letter apart, one importing the other, would be a worse pair than a
slightly longer name.

Making one. :meth:`WindowPaneTreeMixin._make_pane` builds a
``SessionPane`` around an engine factory and :meth:`_make_pane_at` does
the same at an explicit path, for the repo picker's spawn somewhere other
than this app's own ``cwd``; :meth:`_make_group` wraps tabs in a
``PaneGroup``, the window's only leaf kind since v0.97.0. Those first two
were the ``-- pane plumbing`` banner in doxa/app.py; it is stated here
instead, because both methods it labelled moved out from under it.

Asking what is mounted. :meth:`groups`, :meth:`leaf_tabs`, :meth:`panes`
and :meth:`archived_tabs` are the four inventories, each in DOM order, the
first and third of them answering ``[]`` rather than raising once the
screen stacks are gone. :meth:`_tab_of` goes from a pane back to its tab,
and :meth:`_window_root` finds the outermost ``SplitBox`` -- the window's
layout tree, which through v0.95.0 lived one per tab and now lives once
per window. That last one was all the ``-- item D: persisted tab set``
banner in doxa/app.py had left to label once :mod:`doxa.appwindow.restore`
took the rest of it, so that banner is stated here too rather than left
standing over a single method.

Asking which strip. A window had exactly one ``TabbedContent`` through
v0.95.0 and an id was the right way to name it; it has N now, one per
group, and "the strip" is a question about which group, always.
:meth:`tabbed_of` answers it for a widget or for the focused group,
:meth:`_strip` is the raising drop-in for the old ``query_one``,
:meth:`tabbed_holding` finds the strip that holds a given tab id across
every group and :meth:`_strip_for` falls back from it to the focused
group's. :meth:`refresh_strip_visibility` puts every strip where its tab
count now says it belongs, and re-pins the transcripts underneath that
would otherwise be left one row short of the tail.

Asking who has the keyboard, which is most of this module.
:meth:`_focused_node` is the guarded ``App.focused`` every other question
starts from; :meth:`focused_group` names the one group,
:meth:`focused_pane` the one session, :meth:`active_pane` the session a
keystroke MEANS (the two differ exactly where a v0.92.0 diff is
concerned), :meth:`focused_surface` the leaf of whatever kind, and
:meth:`_active_tab` the focused group's active tab when it is one restore
cares about. :meth:`_focus_tab` is the ONE place that decides what
"focused" means for a tab and the one place that does it, with
:meth:`_clear_seen_marks` for the marks that only a keyboard arriving
clears and :meth:`_focus_active_tab` for the callers that set ``active``
by id.

Dividing one. :meth:`split_active_pane` spawns a second session in the
same tab through the same factory Ctrl+T uses, and :meth:`toggle_diff_pane`
reuses that machinery verbatim with one different widget in the new half
-- session left, diff right, both live -- with :meth:`diff_pane_for` for
"does this session already have one open".

Measuring it. :meth:`_pane_regions` is every VISIBLE surface's painted
rectangle, which is what makes :meth:`focus_pane_towards` move the
keyboard to somewhere the user can actually see, and
:meth:`grow_pane_towards` moves the divider between a surface and its
neighbour.

The twenty-nine move verbatim -- same order, same docstrings, same
comments, byte for byte -- after :mod:`doxa.appwindow.failures`,
:mod:`doxa.appwindow.restore`, :mod:`doxa.appwindow.sidebar` and
:mod:`doxa.appwindow.tabs`. Not one of them carries a decorator Textual
reads: ``active_pane``'s ``@property`` is a plain descriptor and came
along, and an ``@on`` handler could not have come at all --
``_MessagePumpMeta`` registers handlers by scanning the class body it
builds, and never scans a plain mixin's. That is why two of them stayed
behind in the MIDDLE of this family: ``_strip_visibility_on_tab_activated``,
which is :meth:`refresh_strip_visibility`'s second door, and
``_hold_focus_for_a_blocking_dialog``, which is the net under
:meth:`_focus_tab`. Every ``action_*`` and ``_cmd_*`` that calls in here
stayed for a different reason -- they are the actions family, and they
move with it rather than ahead of it -- and so did ``DIVIDER_STEP``, a
class attribute of the window like ``BINDINGS`` beside it.

Nothing had to be re-levelled: not one of the twenty-nine carries a
deferred import, so every moved line is identical to the character. No
constant moved and nothing is re-exported, and nothing the suite patches
on ``doxa.app`` is read by anything that did -- the free names these
methods need are ``contextlib``, ``layout_mod``, ``split_mod`` and the
widget classes imported below, and ``SessionEngine``, ``notify_mod``,
``ChipPicker``, ``ClockChip``, ``ReasoningSection`` and ``_stop_session``
are none of them.
"""

from __future__ import annotations

import contextlib
from typing import Any, Callable

from textual.app import ScreenStackError
from textual.css.query import NoMatches
from textual.widgets import TabbedContent

from .. import layout as layout_mod
from ..session.pane import SessionPane
from ..ui import split as split_mod
from ..ui.diffview import DiffPane
from ..ui.prompt import PromptInput
from ..ui.split import PaneGroup, PaneTab, SplitBox
from ..ui.transcript import ArchivedSessionTab, SubagentTranscriptTab


class WindowPaneTreeMixin:
    """DoxaApp's pane-tree half. Mixed into the app, never used
    standalone: every method here reads window state through ``self`` --
    the mounted groups and their strips, the panes and surfaces inside
    them, the group the keyboard was last seen in, and the serials that
    name the next pane and the next group."""

    def _make_pane(self, engine_factory: "Callable[[], Any]") -> SessionPane:
        return SessionPane(
            self._tab_title(), self.cwd, self.model, engine_factory,
        )

    def _make_pane_at(
        self, path: str, engine_factory: "Callable[[], Any]"
    ) -> SessionPane:
        """Item 4's own ``_make_pane``: same shape, an explicit ``path``
        standing in for this app's own ``cwd`` everywhere it matters (the
        born-title AND the pane's own ``cwd`` fallback -- see SessionPane.
        _boot's "engine cwd wins over the pane's own" comment for why the
        engine's real cwd is still what ultimately decides the GitLine/tab
        label once it boots; this is only the correct BEFORE-boot guess)."""
        return SessionPane(self._tab_title(path), path, self.model, engine_factory)

    def groups(self) -> "list[PaneGroup]":
        """Every pane group in the window, in DOM order.

        DOM order is NOT the order ``Ctrl+1``..``Ctrl+9`` counts in --
        that is :meth:`_group_order`, derived from the painted rectangles,
        because what the user counts is what is on screen. Every caller
        that needs the numbering says so by calling the other one.

        ``[]`` rather than a raise once the window is gone, for the reason
        :meth:`_focused_node` gives: ``App.query`` resolves against
        ``self.screen`` (``DOMNode.query`` -> ``_get_dom_base``), so a
        handler drained after the screen stacks are cleared would get a
        ``ScreenStackError`` out of what reads as a plain inventory
        question. "No groups" is the true answer for a window that no
        longer exists, and it is the one every caller here already
        handles."""
        try:
            return list(self.query(PaneGroup))
        except ScreenStackError:
            return []

    def refresh_strip_visibility(self) -> None:
        """Put every group's tab strip where its TAB COUNT now says it
        belongs, and keep the transcripts underneath from jumping.

        A strip appearing takes a row away from the pane below it, and a
        transcript that was pinned to its newest block would be left one
        row short of it -- the same "the scroll was lost, not the output"
        defect ``SessionPane.scroll_transcript_to_end`` exists for, in its
        layout form. So the panes that were AT the tail are asked FIRST
        (afterwards the question is unanswerable -- they are one row off
        either way, and a pane the user had deliberately scrolled up in
        must not be dragged to the bottom), and re-pinned only when a
        strip actually moved. A group whose visibility did not change
        pays nothing.

        Every group, not just the one that gained or lost a tab: moving a
        tab between groups (:meth:`move_tab_to_group`) changes two counts
        at once, and closing the last tab of a group removes a third
        thing. One loop over a handful of widgets, on a tab lifecycle
        event, is cheaper than being wrong about which group to visit.

        Every TAB's leaves, not just the group's visible one: opening a
        second tab ACTIVATES it, so the pane that is about to lose a row
        is already in the background by the time this runs. Its
        ``#block-list`` still answers for where it was standing (see
        ``SessionPane.transcript_at_end``), and ``scroll_transcript_to_end``
        leaves ``_tail_pending`` behind for its next Show rather than
        scrolling a box that is not on screen.

        TWO doors reach this, and neither alone is complete:
        :meth:`_persist_tabset`, which every tab a restart would bring
        back passes through, and
        :meth:`_strip_visibility_on_tab_activated`, which catches the
        tabs it does not -- a subagent transcript tab is opened and closed
        without persisting anything. A removal that leaves the active tab
        alone posts no activation; a transcript tab writes no record; the
        two together have no gap between them.
        """
        for group in self.groups():
            # The cheap question first. This runs on every tab ACTIVATION
            # as well as every tab lifecycle event, and a tab switch moves
            # no strip at all -- so a group that is already where it
            # belongs never pays for the per-leaf scan below.
            pinned = (
                self._pinned_transcripts(group)
                if group.strip_should_hide() != group.has_class("-strip-hidden")
                else ()
            )
            if not group.refresh_strip():
                continue
            for surface in pinned:
                # AFTER the refresh, never during it. The strip has only
                # just been told to appear; the row it takes has not been
                # taken yet, so a re-pin issued here would compute the tail
                # against the geometry the pane is about to lose and land
                # one row short of the one it is about to get -- measured,
                # and the exact defect this loop exists to prevent. One
                # frame later the pane is either painted (and is scrolled)
                # or is a background tab (and gets ``_tail_pending`` for
                # its next Show), which is the fork
                # ``scroll_transcript_to_end`` already owns.
                with contextlib.suppress(Exception):
                    self.call_after_refresh(surface.scroll_transcript_to_end)

    def _focused_node(self) -> "Any | None":
        """``App.focused`` -- the widget Textual says holds the keyboard --
        or ``None`` when there is no window left to ask.

        Every "which group / which pane / which surface" question below
        starts here, and every one of them is read from MESSAGE HANDLERS.
        That is what makes the raw property the wrong thing to call:
        ``App.focused`` reads ``self.screen``, and at teardown Textual
        clears the screen stacks in ``App._close_all`` and only THEN drains
        the app's own message queue (``_close_messages``). Any handler
        still in flight in that window -- the ``events.DescendantFocus``
        one every closing screen posts as focus comes off its widgets, for
        instance, which :meth:`_hold_focus_for_a_blocking_dialog` listens
        for -- runs against an app whose ``self.screen`` raises
        ``ScreenStackError``, and ``self.focused`` raises with it.

        Through v0.96.0 nothing met that: ``active_pane`` asked the STRIP
        first (``_active_tab``, which swallows) and answered None without
        ever reading ``self.focused``. v0.97.0 made the GROUP the first
        question -- correctly, since "the active tab" is a question about
        a group now -- and thereby moved an unguarded ``self.focused`` in
        front of every ``active_pane`` caller, the error surface included:
        :meth:`_failure_surface` calls ``active_pane`` to decide where to
        DRAW the block, so the raise took out the report of itself as
        well, escaped ``_process_messages`` and failed whichever test was
        running. Measured as exactly that, in the full suite only, where a
        busier loop lands the late event after the stacks are gone:
        tests/test_derive.py::test_looking_at_the_tab_clears_the_staged_tint
        failing at ``run_test`` EXIT with two ``ScreenStackError``s -- the
        handler's, and the surface's on top of it.

        So the guard belongs here rather than in the handler: "nothing
        holds the keyboard" is the honest answer for a window that no
        longer exists, it is the answer every caller was already written
        against, and putting it in one accessor keeps the next handler
        that asks after teardown from having to know any of this."""
        try:
            return self.focused
        except ScreenStackError:
            return None

    def focused_group(self) -> "PaneGroup | None":
        """The ONE group that holds the keyboard.

        Exactly one, which is the pane-groups spec's own focus rule: the
        status bar reflects that group's active tab, and every key that
        means "this tab" means a tab of this group. Derived from
        ``self.focused`` rather than from a flag this app maintains, for
        the reason :meth:`focused_pane` gives -- a flag is a second answer
        to a question the framework already answers, and the two drifting
        apart is how the v0.32.0 restored-active-tab defect happened.

        Falls back to the remembered last group, then to the first in
        reading order, for the case focus is legitimately somewhere that is
        not a group at all (a modal, the command palette, the rename
        field): a window always has an answer to "which group", and jumping
        to a different one while a dialog is up would move the user's work
        under them.

        **The DOM wins, and the remembered id is only a fallback.** That
        is a deliberate choice against the obvious alternative, which was
        tried and reverted. ``Widget.focus()`` is deferred in Textual 5.3
        (it schedules ``screen.set_focus`` with ``call_next``), so for one
        message-pump turn after a split this answers with the group the
        user came FROM -- and it would be tempting to believe
        :meth:`_focus_tab`'s synchronously-recorded intent instead, the way
        ``PaneTab.focused_leaf`` is believed one level down.

        Measured, that costs more than it buys. A group that has not been
        painted yet has a zero-area rectangle, so trusting the intent makes
        the NEXT ``split_active_pane`` in the same turn refuse ("not enough
        height to split: each pane needs 9 rows and this one has 0") and
        makes :meth:`active_pane` answer with a pane from a group the
        keyboard has demonstrably not reached. The window it would fix is
        one transient write that corrects itself the moment the new pane
        boots (``_note_pane_booted`` persists again), so the trade is a
        real refusal against a record nobody reads."""
        node: Any = self._focused_node()
        while node is not None:
            if isinstance(node, PaneGroup):
                self._last_group_id = node.id or self._last_group_id
                return node
            node = node.parent
        remembered = self._last_group_id
        groups = self.groups()
        if remembered:
            for group in groups:
                if group.id == remembered and group.is_mounted:
                    return group
        return next(iter(self._group_order() or groups), None)

    def tabbed_of(self, widget: "Any" = None) -> "TabbedContent | None":
        """The tab strip that owns ``widget``, or -- given nothing -- the
        FOCUSED group's own.

        This is what replaced ``query_one("#session-tabs")`` everywhere in
        this file. Through v0.95.0 a window had exactly one strip and an id
        was the right way to name it; a window has N now, and "the strip"
        is a question about which group, always. Returns ``None`` rather
        than raising, because most callers were already inside a
        ``contextlib.suppress`` for the mid-teardown case and the ones that
        were not read better with an explicit branch."""
        if widget is not None:
            return split_mod.tabbed_of(widget)
        group = self.focused_group()
        if group is None or not group.is_mounted:
            return None
        try:
            tabbed = group.tabbed
        except Exception:  # noqa: BLE001 -- group not composed yet
            return None
        # query_one succeeding is not the same as mounted -- the guard
        # SessionPane._system needed for exactly this, in v0.91.0.
        return tabbed if tabbed.is_mounted else None

    def _strip(self) -> TabbedContent:
        """The FOCUSED group's tab strip, raising when there is none.

        The drop-in for ``query_one("#session-tabs", TabbedContent)``: it
        raises the same way in the same states (nothing mounted, app
        mid-teardown), so every caller that was already wrapped in a
        ``contextlib.suppress`` keeps behaving exactly as it did."""
        group = self.focused_group()
        if group is None:
            raise NoMatches("no pane group is mounted")
        return group.tabbed

    def _strip_for(self, tab_id: str) -> TabbedContent:
        """The strip that HOLDS this tab, falling back to the focused
        group's.

        The fallback is not a shrug: a tab id that no strip holds is a tab
        being created (``add_pane`` has not landed) or one already removed,
        and in both cases the focused group is the only group the caller
        could have meant. Getting this wrong the other way -- defaulting to
        the focused group FIRST -- would let a status write aimed at a
        background group's tab land on the foreground one's."""
        holder = self.tabbed_holding(tab_id)
        return holder if holder is not None else self._strip()

    def tabbed_holding(self, tab_id: str) -> "TabbedContent | None":
        """The strip that holds the tab with this ID, across every group.

        The lookup the tab-status writers need (:func:`doxa.ui.labels.
        _write_tab_class` and friends): a pane writes ``-working`` onto its
        own header, and with N strips "the strip" no longer names one."""
        if not tab_id:
            return None
        for group in self.groups():
            try:
                tabbed = group.tabbed
            except Exception:  # noqa: BLE001 -- not composed yet
                continue
            if not tabbed.is_mounted:
                continue
            with contextlib.suppress(Exception):
                if tabbed.get_pane(tab_id) is not None:
                    return tabbed
        return None

    def _make_group(self, *tabs: "Any", active_id: "str | None" = None) -> PaneGroup:
        """One pane group holding ``tabs``. The window's only leaf kind."""
        self._group_serial += 1
        return PaneGroup(
            *tabs, active_id=active_id, id=f"group-{self._group_serial}"
        )

    def leaf_tabs(self) -> "list[PaneTab]":
        return list(self.query(PaneTab))

    def _tab_of(self, pane: "Any") -> "PaneTab | None":
        return getattr(pane, "tab", None) if isinstance(pane, SessionPane) else None

    def focused_pane(self) -> "SessionPane | None":
        """The ONE pane per window that holds the keyboard.

        Derived from ``self.focused`` -- the widget Textual says has focus
        -- rather than from a flag this app maintains, because a flag is a
        second answer to a question the framework already answers, and the
        two drifting apart is precisely how the v0.32.0 restored-active-tab
        defect happened. Falls back to the active tab's last focused leaf
        for the case where focus is legitimately somewhere that is not a
        pane at all (a modal, the command palette, the rename field): the
        status bar still has to reflect ONE pane, and jumping to the
        tab's first leaf while a dialog is up would move it under the
        user."""
        node: Any = self._focused_node()
        while node is not None:
            if isinstance(node, SessionPane):
                return node
            if isinstance(node, DiffPane):
                # The keyboard is in a diff. "Which session does this
                # keystroke mean" still has an answer, and it is the
                # session the diff is OF -- a key aimed at a session,
                # pressed while looking at that session's diff, is aimed
                # at that session. Falling through to the tab's last
                # focused leaf would usually give the same answer and
                # would give a WRONG one in a tab holding two sessions.
                owner = node.session_pane()
                if owner is not None:
                    return owner
                break
            node = node.parent
        # Focus is somewhere that is not a leaf at all (a modal, the
        # command palette, the rename field). The FOCUSED GROUP's active
        # tab is the answer: the status bar still has to reflect ONE pane,
        # and moving it to some other group's while a dialog is up would
        # change the subject under the user.
        group = self.focused_group()
        tab = group.active_tab() if group is not None else None
        if isinstance(tab, PaneTab):
            leaf = tab.focused_leaf
            if isinstance(leaf, SessionPane) and leaf.is_mounted:
                return leaf
            return next(iter(tab.leaves()), None)
        return None

    @property
    def active_pane(self) -> SessionPane | None:
        """The session the user is driving: the FOCUSED leaf of the active
        tab (v0.91.0), which through v0.88.0 was the same thing as the
        active tab because a tab held exactly one pane.

        Every engine-touching caller in this file reads this -- the
        palette's actions, ``/mode``, the status refresh, the failure
        surface. With two panes visible, "the tab that is showing" is no
        longer an answer to "which session does this keystroke mean", and
        the pane holding the keyboard is: a key aimed at a session is
        aimed at the session you are typing into.

        **v0.97.0: :meth:`focused_pane` wins outright**, where through
        v0.95.0 its answer was cross-checked against the active tab and
        discarded if it belonged to another one. That check existed because
        a TAB held several panes and the active tab bounded the question.
        With the keyboard now able to sit in a v0.92.0 diff that is a tab
        of its OWN group, the check started discarding the right answer:
        the session a diff is of lives in a different group, ``pane.tab is
        tab`` was false, and ``active_pane`` came back None while a session
        was plainly on screen and being typed at. ``focused_pane`` already
        resolves the diff case deliberately (see its ``DiffPane`` branch);
        second-guessing it here was the defect -- but only for a pane in
        ANOTHER group. Inside the focused group the active tab still bounds
        the question, and it has to: a read-only tab showing (an archived
        session, a subagent transcript) means there IS no session pane
        here, and every caller reads that None as "ask
        ``_close_read_only_tab`` instead". Returning the live pane whose
        prompt still happened to hold focus made Ctrl+Q on an archived tab
        end the neighbouring session -- caught by
        tests/test_restore_view.py, which is exactly the pair of tests that
        distinction exists for."""
        group = self.focused_group()
        tab = group.active_tab() if group is not None else None
        pane = self.focused_pane()
        if (
            pane is not None
            and pane.is_mounted
            and split_mod.group_of(pane) is not group
        ):
            # The keyboard is in some other group -- a v0.92.0 diff is the
            # only way that happens, and the session it is a diff OF is the
            # right answer (focused_pane's own DiffPane branch decided so).
            return pane
        if not isinstance(tab, PaneTab):
            return None
        if pane is not None and pane.is_mounted and pane.tab is tab:
            return pane
        leaf = tab.focused_leaf
        if isinstance(leaf, SessionPane) and leaf.is_mounted:
            return leaf
        return next(iter(tab.leaves()), None)

    def panes(self) -> list[SessionPane]:
        """Every session leaf in the window, in DOM order -- across tabs
        AND across the splits inside one tab. Unchanged as a query; what
        changed is that one tab can now contribute more than one.

        Empty rather than raising on a window that is gone -- the same
        guard, and the same reasoning, as :meth:`groups`. This one is the
        load-bearing case: :meth:`_failure_surface` falls back to it when
        there is no active pane, so an unguarded query here would be the
        error surface failing on the one path it exists for."""
        try:
            return list(self.query(SessionPane))
        except ScreenStackError:
            return []

    def archived_tabs(self) -> "list[ArchivedSessionTab]":
        """Restored tabs whose session is gone (v0.32.0) -- read-only
        transcript tabs, deliberately NOT part of :meth:`panes`, which
        every caller in this file reads as "tabs with a session behind
        them" and must keep reading that way."""
        return list(self.query(ArchivedSessionTab))

    def _active_tab(self) -> "PaneTab | ArchivedSessionTab | None":
        """The FOCUSED GROUP's active tab, when it is one restore CARES
        about -- either kind. ``active_pane`` stays SessionPane-only on
        purpose (every engine-touching caller depends on that); this is
        the one question that spans both.

        "The active tab" is a question about a group since v0.97.0, and
        every caller of this means the group holding the keyboard: which
        tab the status bar reflects, which one Ctrl+W closes, which one
        the record calls active."""
        try:
            tab = self._strip().active_pane
        except Exception:
            return None
        return tab if isinstance(tab, (PaneTab, ArchivedSessionTab)) else None

    def _focus_tab(self, tab: "Any", *, retry: bool = True) -> None:
        """Put the keyboard into TAB. The ONE place that decides what
        "focused" means for a tab, and the one place that does it.

        Until v0.38.0 nothing called this because nothing had to: a
        ``SessionPane`` focused its own prompt in ``on_mount``, and since
        focusing a widget inside a ``TabPane`` also ACTIVATES that pane
        (``TabbedContent._on_tab_pane_focused``), activation was a side
        effect of mounting -- it landed whenever Textual got round to the
        mount, which is a race against anything else deciding which tab is
        active. Focus now follows EXPLICIT user intent instead, so every
        site that moves the user to a tab on purpose calls this: Ctrl+T
        (:meth:`action_new_tab`), Ctrl+←/→ (:meth:`_cycle_tab`), the
        palette's tab entries and the peer chip's jump
        (:meth:`_switch_to_tab`), the repo picker's new tab
        (:meth:`open_tab_at`), and startup/restore
        (:meth:`_activate_initial_tab`). :meth:`_on_tab_activated` calls
        it too -- a MOUSE click on a tab produces no key event and has no
        handler of its own to hang this on, so the event is the only hook
        that path has.

        A ``SessionPane`` focuses its prompt; the two read-only kinds
        (``ArchivedSessionTab``, ``SubagentTranscriptTab``) focus their own
        ``.scroll`` container instead, so keyboard scrolling works the
        moment you land on one -- v0.85.0, and load-bearing beyond that
        single convenience: :meth:`_cycle_tab` landing on a tab with
        NOTHING focusable left Textual's own ``AUTO_FOCUS`` (``App.
        AUTO_FOCUS = "*"``, fired on the next screen-resume tick while
        ``self.focused`` is ``None``) to pick the first focusable widget
        it could find ANYWHERE in the DOM -- unscoped by which tab is
        actually visible, so it landed back on the SessionPane's own
        prompt in a different, now-hidden tab. Focusing that prompt posts
        ``TabPane.Focused`` right back up to ``TabbedContent``, which
        reactively reassigns ``active`` to ITS tab -- so the cycle
        silently reverted itself one message-pump turn later, the exact
        shape of the reported defect ("only seems to work ... not between
        read-only finished sessions"). Giving every tab kind SOMETHING
        focusable closes the gap AUTO_FOCUS was falling into, at the
        source, for every caller in the list above, not just cycling.

        **v0.91.0: a tab may hold several panes, so this needs to name
        ONE.** It takes the tab's remembered focused leaf -- the pane the
        keyboard was in the last time the user was in this tab -- rather
        than its first, because coming back to a split tab and landing
        somewhere other than where you left is the same class of surprise
        as restoring onto the wrong tab. A brand-new tab's remembered leaf
        is the one it was built with. Accepts a PANE as well as a tab, for
        the callers that already have the leaf they mean (a split, a
        directional move)."""
        # Which GROUP this focus move means, remembered for the case
        # ``self.focused`` stops naming a group at all -- a modal, the
        # command palette, the rename field. NOT believed over the DOM:
        # see :meth:`focused_group` for the measurement that settled that.
        group = split_mod.group_of(tab)
        if group is not None and group.id:
            self._last_group_id = group.id
        if isinstance(tab, SessionPane):
            owner = tab.tab
            if isinstance(owner, PaneTab):
                owner.focused_leaf = tab
            try:
                tab.query_one("#prompt-input", PromptInput).focus()
            except Exception:  # noqa: BLE001 -- see below
                # A leaf mounted THIS turn has not composed its own
                # subtree yet: ``mount`` resolves when the widget is in
                # the DOM, and its children arrive on the next
                # message-pump turn. Suppressing that used to be
                # harmless, because the only caller was acting on a tab
                # that had been on screen for a while; a SPLIT focuses a
                # pane it created a moment ago, and swallowing the miss
                # would leave the keyboard in the pane the user split
                # AWAY from -- silently, and only sometimes. So the
                # intent is re-stated on the next refresh instead of
                # dropped.
                #
                # ONCE, and the bound is load-bearing rather than
                # defensive: a pane being torn down never grows a prompt,
                # so an unbounded re-state is a callback that schedules
                # itself every refresh forever -- an app that never goes
                # idle, which is the busy-idle bug GitLine's docstring
                # warns about with a tighter loop.
                if retry:
                    self.call_after_refresh(self._focus_tab, tab, retry=False)
                return
            self._clear_seen_marks(tab)
            return
        if isinstance(tab, PaneTab):
            leaf = tab.focused_leaf
            if not (isinstance(leaf, SessionPane) and leaf.is_mounted):
                leaf = next(iter(tab.leaves()), None)
            if leaf is not None:
                self._focus_tab(leaf)
            return
        if isinstance(tab, (ArchivedSessionTab, SubagentTranscriptTab)):
            with contextlib.suppress(Exception):
                tab.scroll.focus()

    def _clear_seen_marks(self, pane: "SessionPane") -> None:
        """"You are looking at this now" -- for the ONE pane that just got
        the keyboard, and never for its visible siblings (v0.91.0).

        The three affordances (`-done-unseen`, the needs-input blink, the
        `-staged` tint) all cleared on tab ACTIVATION through v0.88.0,
        which was the same event as "this pane got the keyboard" while a
        tab held one pane. It is not the same event any more, and the spec
        settles which of the two it should follow: the marker means *you
        have not looked at this*, and a pane in the corner of a 2x2 grid
        may genuinely be unread. So visible-but-unfocused does NOT count
        as seen; only focus clears. The panes beside it keep their marks
        until the keyboard actually arrives there."""
        pane._set_tab_class("-done-unseen", False)
        pane.set_needs_input(False)
        pane.set_staged(False)

    def _focus_active_tab(self) -> None:
        """:meth:`_focus_tab` for whichever tab is active RIGHT NOW --
        for the callers that set ``TabbedContent.active`` by id and would
        otherwise have to look the pane back up themselves. Safe to call
        immediately after that assignment: ``active`` is a plain reactive,
        so ``active_pane`` resolves synchronously once it is set (it is
        only the INITIAL value that arrives late -- see
        :meth:`_activation_pending`)."""
        with contextlib.suppress(Exception):
            tabbed = self._strip()
            self._focus_tab(tabbed.active_pane)

    def _window_root(self) -> "SplitBox | None":
        """The OUTERMOST :class:`~doxa.ui.split.SplitBox` on the screen --
        the window's layout tree, which through v0.95.0 lived one per tab
        and now lives once per window."""
        for box in self.query(SplitBox):
            if not isinstance(box.parent, SplitBox):
                return box
        return None

    async def split_active_pane(self, orientation: str) -> "str | None":
        """Divide the focused pane. Returns a refusal to show the user, or
        ``None`` when it happened.

        Two independent sessions side by side -- the spec's own reading of
        its second open question -- so this spawns through the SAME
        ``new_session_factory`` Ctrl+T uses. A split is a new session in
        the tab you are already in, not a second view onto the one that is
        there.

        **Focus goes to the NEW pane, and it goes there explicitly.** A
        leaf mounts unfocused (v0.38.0's rule, which splits inherit rather
        than re-litigate) and whatever creates it says where the keyboard
        goes; a user who just asked for a second pane is asking to work in
        it, the same way Ctrl+T's new tab takes the keyboard. The pane it
        was split off keeps rendering, keeps streaming, and keeps any
        "you missed something" mark it had -- visible is not focused, and
        neither is seen.

        Refused, with a message and no change at all, when the resulting
        panes would be below the floor (:func:`doxa.layout.split_refusal`)
        or when this pane has already spent its depth allowance
        (:data:`doxa.layout.SPLIT_SLOTS`). A refusal that performed a
        sliver would be worse than the refusal."""
        group = self.focused_group()
        if group is None:
            return "there is no pane group here to split"
        box = split_mod.free_box(group)
        if box is None:
            return (
                f"this pane is already split as deep as DOXA goes "
                f"({layout_mod.SPLIT_SLOTS} levels) — close a pane, or "
                "split one of its neighbours instead"
            )
        region = group.region
        refusal = layout_mod.split_refusal(region.width, region.height, orientation)
        if refusal is not None:
            return refusal
        new_pane = self._make_pane(self._new_session_factory)
        new_group = self._make_group(self._make_tab(new_pane))
        await box.mount(split_mod.chain(new_group))
        box.divide(orientation)
        self._focus_tab(new_pane)
        self._persist_tabset()
        return None

    def diff_pane_for(self, session_id: str) -> "DiffPane | None":
        """This session's diff leaf, if it has one open anywhere."""
        for pane in self.query(DiffPane):
            if pane.session_id == session_id:
                return pane
        return None

    async def toggle_diff_pane(
        self, pane: "SessionPane | None" = None,
    ) -> "str | None":
        """Open a session's live diff BESIDE it, or close it. Returns a
        refusal to show the user, or ``None`` when it happened.

        ``pane`` defaults to the focused session -- what F2 and ``/diff``
        mean by "this session". v1.0.1 gives it a caller-supplied
        alternative for the two doors that are aimed at a PARTICULAR
        session rather than at the keyboard's: the status chip (which is
        painted inside one pane's own bar) and the ``auto_diff``
        auto-open (whose tick can arrive from a session in a background
        tab, which is precisely the session it must not diff the wrong
        neighbour of).

        This is the spec's design check on v0.91.0's split, run for real:
        *session left, diff right, both live*. It reuses
        :meth:`split_active_pane`'s machinery verbatim -- the same free
        box, the same :func:`doxa.layout.split_refusal` floor, the same
        ``ROW`` orientation -- and differs in exactly one line, the
        widget that goes into the new half. Nothing about the split had
        to be special-cased for a non-session leaf ONCE
        :attr:`doxa.layout.Leaf.view` existed, which is the honest
        version of "the split could express it".

        Toggling closes rather than refusing a second one: a session has
        one diff, per the spec's answer to its own third open question
        (per-session, matching the isolation model -- two sessions in
        worktrees off the same branch have two different diffs)."""
        pane = pane if pane is not None else self.active_pane
        if pane is None:
            return "there is no session pane here to diff"
        tab = pane.tab
        if tab is None:
            return "this pane is not in a tab yet"
        existing = self.diff_pane_for(pane._session_id)
        if existing is not None:
            if existing.queued:
                return (
                    f"{len(existing.queued)} rejection(s) are still queued "
                    "in this diff — they apply when the turn ends. closing "
                    "the pane now would discard them."
                )
            await self._close_group_tab(existing)
            self._focus_tab(pane)
            self._persist_tabset()
            return None
        return await self._open_diff_beside(pane)

    def _pane_regions(self) -> "dict[str, tuple[int, int, int, int]]":
        """Every VISIBLE surface's painted rectangle, keyed by widget id.

        Painted, not structural: the spec's testing bar says a split must
        render two panes with non-zero width and height, because the
        invisible-button defect passed every structural assertion for a
        whole release. Directional focus reads the same rectangles the
        user is looking at, so a pane that is not actually on screen is
        not a destination.

        Across every GROUP since v0.97.0, and only each group's ACTIVE tab:
        an inactive tab is mounted and running but is not painted, and the
        two facts have to stay apart here -- reading every tab would let
        ``Ctrl+Shift+→`` land the keyboard somewhere the user cannot see,
        which is the invisible-button defect in its keyboard form."""
        out: "dict[str, tuple[int, int, int, int]]" = {}
        for group in self.groups():
            # surfaces(), not leaves(): v0.92.0's diff pane is a surface
            # you can focus and scroll, so "rectangles the keyboard can
            # move to" is not the same list as "sessions".
            for leaf in group.surfaces():
                region = leaf.region
                if region.width > 0 and region.height > 0 and leaf.id:
                    out[leaf.id] = (region.x, region.y, region.width, region.height)
        return out

    def focus_pane_towards(self, direction: str) -> bool:
        """Move the keyboard to the geometrically adjacent pane. Returns
        whether it moved -- ``False`` at the edge of the layout, which is
        deliberately silent: an arrow key that has nowhere to go should do
        nothing, not complain."""
        here = self.focused_surface()
        if here is None or not here.id:
            return False
        target_id = layout_mod.neighbour(self._pane_regions(), here.id, direction)
        if target_id is None or target_id == here.id:
            return False
        # Across every GROUP (v0.97.0): the rectangles the keyboard can
        # move to are the ACTIVE tab of each region, which is exactly what
        # _pane_regions just answered with.
        surfaces = [
            surface for group in self.groups() for surface in group.surfaces()
        ]
        target = next((p for p in surfaces if p.id == target_id), None)
        if target is None:
            return False
        if isinstance(target, DiffPane):
            # A diff has no prompt to focus, so _focus_tab's "focus the
            # pane's prompt" contract does not apply; the widget itself
            # takes the keyboard, which is what makes it scrollable.
            target.focus()
            return True
        self._focus_tab(target)
        return True

    def focused_surface(self) -> "Any | None":
        """The LEAF holding the keyboard, of whatever kind.

        The geometric twin of :meth:`focused_pane`, which answers the
        different question "which session does this keystroke mean" and
        deliberately keeps answering with a session even while the
        keyboard is in a diff."""
        node: Any = self._focused_node()
        while node is not None:
            if isinstance(node, (SessionPane, DiffPane)):
                return node
            node = node.parent
        return self.focused_pane()

    def grow_pane_towards(self, direction: str) -> bool:
        """Alt+arrow: move the divider BETWEEN this pane and its
        neighbour, growing this pane in ``direction``.

        Finds the nearest ancestor split whose orientation matches the
        axis being asked about, and nudges the boundary on this pane's
        side of it. A drag changes weights and weights persist, so this
        writes the tab set like any other layout change.

        Reads :meth:`focused_surface`, not :meth:`active_pane`: with the
        keyboard in a v0.92.0 diff leaf, Alt+← must widen the DIFF, not
        the session it is a diff of. This is the gesture the live-diff
        spec asks for by name ("a left/right split needs the sibling
        gesture" to Ctrl+Up/Down) and it needed no new key at all --
        v0.91.0 had already built it; it only had to stop assuming every
        leaf was a session."""
        pane = self.focused_surface()
        if pane is None:
            return False
        want = (
            layout_mod.ROW if direction in ("left", "right")
            else layout_mod.COLUMN
        )
        # Start from the GROUP, not the surface (v0.97.0): the boxes that
        # divide the window sit above the group, and a surface's own parent
        # chain now runs through its tab and its strip first. Reading
        # ``focused_surface`` and then climbing from the pane -- what this
        # did through v0.95.0 -- found no SplitBox at all and the key went
        # silently dead, which is how it was caught.
        node: Any = split_mod.group_of(pane) or pane
        parent = node.parent
        while isinstance(parent, SplitBox):
            if parent.is_used and parent.orientation == want:
                kids = list(parent.children)
                index = kids.index(node)
                forward = direction in ("right", "down")
                # Growing FORWARD means pushing the divider after this
                # child; growing BACKWARD means pulling the divider before
                # it, which is the same divider seen from the other side.
                moved = (
                    parent.nudge(index, self.DIVIDER_STEP) if forward
                    else parent.nudge(index - 1, -self.DIVIDER_STEP)
                )
                if moved:
                    self._persist_tabset()
                return moved
            node = parent
            parent = node.parent
        return False

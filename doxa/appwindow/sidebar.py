# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.appwindow.sidebar -- the session rail, and the groups it indexes.

v1.0.0 gave the window a rail down its side, and what it lists is a session
INDEX rather than a second tab strip: every session this window knows
about, including the detached and ended ones no group is showing any more.
The rail is a SIBLING of the window root and never a member of it --
nothing in this module touches the layout tree, and nothing in the tree
knows the rail exists. That separation is the feature's whole design (see
:mod:`doxa.ui.sidebar`), and it is why ``DoxaApp._window_root`` needed no
change at all when the rail arrived.

Geometry first. :meth:`WindowSidebarMixin.sidebar` finds the widget or says
there is no window left to ask, :meth:`sidebar_width` clamps the width a
drag or the settings registry proposes, :meth:`_window_width`,
:meth:`_narrowest_group` and :meth:`_narrowest_group_unrailed` measure the
room there is for one, and :meth:`sidebar_refusal` prices opening and
resizing through ONE function -- a drag refusing at a looser floor than
``F3`` does would build an arrangement nothing else could reproduce.
:meth:`sidebar_has_something_to_say` and :meth:`sidebar_should_show` are
hide-at-zero.

Then the model. :meth:`_sidebar_order` names the sessions and their order,
:meth:`_sidebar_pane` and :meth:`_sidebar_surfaces` map them to whatever is
mounted, :meth:`_sidebar_panes` reads this window's pane groups as the
rail's entries, and :meth:`sidebar_rows` hands all of it to
:func:`doxa.ui.sidebar.build_rows`, the pure function that decides what a
rail shows. Painting is :meth:`refresh_sidebar` -- the whole derivation,
once per tab lifecycle event -- beside :meth:`refresh_sidebar_marks`, which
is one row per mark toggle at blink rate and exists so a mark never costs a
rebuild. :meth:`set_sidebar`, :meth:`resize_sidebar` and
:meth:`_nudge_sidebar` are the writes, :meth:`notify_sidebar` is where a
refusal reaches the user, and :meth:`reveal_session`,
:meth:`focus_group_by_key` and :meth:`toggle_group_expanded` are the three
gestures a row offers.

The collections half is one shape five times over, which is what
:meth:`_apply_collections` is: apply the pure function from
:mod:`doxa.collections`, keep its note, repaint, persist. The MODEL refuses
(a duplicate name, an unknown one, an empty one) and this layer never
second-guesses it -- the same division :func:`doxa.layout.split_refusal`
keeps from ``DoxaApp.split_active_pane``. That was the ``-- collections``
banner in doxa/app.py; it is stated here instead, because every method it
labelled moved out from under it.

:meth:`_group_order` and :meth:`group_of` come along because the rail is
why a group is numbered off its PAINTED rectangle rather than off tree
order, and so does the ``Ctrl+<digit>`` overlay that counts the same groups
the same way: :meth:`_flash_group_numbers`, :meth:`_cancel_group_flash`,
:meth:`_hide_group_numbers`, :meth:`_dismiss_group_numbers`,
:meth:`focus_group_number` and :meth:`move_tab_to_group`.

The methods move verbatim -- same order, same docstrings, same comments --
as the third of DoxaApp's method families to become a mixin, after
:mod:`doxa.appwindow.failures` and :mod:`doxa.appwindow.restore`. Not one
of the thirty-seven carries a decorator, and what could not come is exactly
what Textual has to meet in the class body it builds: the six
``@on(SessionSidebar.*)`` handlers, ``on_resize`` and ``on_event``, since
``_MessagePumpMeta`` registers handlers by scanning that body and never
scans a plain mixin's. ``action_toggle_sidebar``,
``action_sidebar_wider`` and ``action_sidebar_narrower`` stay for a
different reason -- they are the actions family, and they move with it.
``_pane_ctx``, ``_pane_repo_root`` and ``_describe_session`` stay under the
``-- the session sidebar`` and ``-- the rail's model`` banners, which is
why both are still in doxa/app.py. No constant moved: ``GROUP_FLASH_SECS``
is a class attribute of DoxaApp, read here as ``self.GROUP_FLASH_SECS``
like any other, and ``dir(doxa.app)`` is unchanged.
"""

from __future__ import annotations

import contextlib
from typing import Any

from textual import events
from textual.app import ScreenStackError
from textual.css.query import NoMatches

from .. import collections as collections_mod
from .. import config as config_mod
from .. import layout as layout_mod
from .. import triage as triage_mod
from ..session.pane import SessionPane
from ..ui import labels as labels_mod
from ..ui import split as split_mod
from ..ui.sidebar import Row as SidebarRow, SessionSidebar, build_rows
from ..ui.split import PaneGroup, PaneTab
from ..ui.transcript import ArchivedSessionTab


class WindowSidebarMixin:
    """DoxaApp's session-rail half. Mixed into the app, never used
    standalone: every method here reads window state through ``self`` --
    the rail widget itself, the groups and panes this window has mounted,
    the collections it holds, and the records of what was detached or
    ended this run."""

    def _group_order(self) -> "list[PaneGroup]":
        """Every VISIBLE group in READING order -- left to right, then top
        to bottom -- which is the order ``Ctrl+<digit>`` numbers them in and
        the order the number overlay paints.

        Derived from the painted rectangles, never from tree order: in a
        2x2 grid a tree order that puts the bottom-left region after the
        top-right one is indistinguishable from a bug, and it is the same
        argument :func:`doxa.layout.neighbour` is built on one level down.
        A group with no painted rectangle yet (mounted this frame, or
        collapsed to nothing) is not numbered, for the same reason
        :meth:`_pane_regions` skips it: an unpainted region is not a
        destination.

        Sorted on the TOP EDGE first and the LEFT EDGE second, so a row of
        three above a row of two numbers 1 2 3 / 4 5. Rows are compared by
        their actual y, not bucketed, because DOXA's own splits always
        align: every region in a row shares an edge by construction."""
        painted = [
            (group.region.y, group.region.x, index, group)
            for index, group in enumerate(self.groups())
            if group.region.width > 0 and group.region.height > 0
        ]
        # The DOM index is the final tie-break so the order is total and
        # deterministic even for two regions that somehow share a corner.
        return [group for _y, _x, _i, group in sorted(painted)]

    def group_of(self, widget: "Any") -> "PaneGroup | None":
        """The group a widget sits in."""
        return split_mod.group_of(widget)

    def sidebar(self) -> "SessionSidebar | None":
        """The rail, or ``None`` when there is no window left to ask.

        Guards BOTH conditions v0.97.0 learned to guard: ``NoMatches``
        (the rail is not composed yet) and ``ScreenStackError`` (the
        screen stacks are cleared before the app's queue is drained, so a
        late handler asking a plain inventory question gets a raise) --
        and then ``is_mounted`` on top, because ``query_one`` succeeding
        means the node is in the DOM, not that it is mounted."""
        try:
            rail = self.query_one(SessionSidebar)
        except (NoMatches, ScreenStackError):
            return None
        return rail if rail.is_mounted else None

    def sidebar_width(self) -> int:
        """The rail's width, clamped to what a rail can be: the width a
        drag is showing right now, else the configured one."""
        if self._sidebar_width_override is not None:
            return layout_mod.clamp_sidebar_width(self._sidebar_width_override)
        return config_mod.sidebar_width()

    def _window_width(self) -> int:
        """The whole window's width in cells, or 0 when nothing is
        painted. Read off the SCREEN and not off the rail: a hidden widget
        has no geometry (v0.99.0's whole defect), so measuring the thing
        that is about to be shown is measuring zero."""
        try:
            return int(self.size.width)
        except (ScreenStackError, AttributeError):
            return 0

    def _narrowest_group(self) -> int:
        """The narrowest PAINTED group, in cells -- what
        :func:`doxa.layout.sidebar_refusal` prices the refusal against.

        Painted, never structural: a group with a zero-area rectangle is
        one that has not been laid out yet, and it is not a region the
        rail can take columns from. The same rule ``_pane_regions`` and
        ``_group_order`` state."""
        widths = [
            group.region.width
            for group in self.groups()
            if group.region.width > 0 and group.region.height > 0
        ]
        return min(widths) if widths else 0

    def _narrowest_group_unrailed(self) -> int:
        """The narrowest painted group as it would be with NO rail at all.

        :func:`doxa.layout.sidebar_refusal` takes the tree's width before
        the rail costs it anything -- which is what ``_narrowest_group``
        measures when the rail is hidden, and is exactly what it does NOT
        measure when the rail is already open. Opening asks the question
        once, from the hidden state, so v1.0.0 never had to tell them
        apart; RESIZING asks it from the shown state, and feeding an
        already-shrunk number back in would price the rail's cost twice
        and refuse a width that fits.

        Undoing that shrink is the same proportion the refusal applies:
        the tree got ``total - rail`` of ``total``."""
        narrowest = self._narrowest_group()
        rail = self.sidebar()
        if narrowest <= 0 or rail is None or rail.styles.display == "none":
            return narrowest
        total = self._window_width()
        # A rail that is shown but not yet LAID OUT has no width to undo
        # (v0.99.0: geometry is a property of being painted, not of being
        # displayed), and that degrades to the un-shrunk answer -- which
        # is the same one the hidden case gives and is the conservative
        # half either way.
        tree = total - int(rail.outer_size.width or 0)
        if tree <= 0 or total <= 0:
            return narrowest
        return narrowest * total // tree

    def sidebar_refusal(self, width: "int | None" = None) -> "str | None":
        """Why the rail cannot open -- or cannot be this WIDE -- right now,
        or ``None``.

        ``width`` is the candidate a drag or a key is proposing;
        :meth:`sidebar_width` is the default, which is the question
        ``F3`` asks. **One function answers both**, which is the whole
        point: a drag that refused at a looser floor than opening does
        would let the mouse build an arrangement the app will not create
        interactively, and the arrangement would then be the one thing
        neither ``F3`` nor a restart could reproduce."""
        return layout_mod.sidebar_refusal(
            self._window_width(),
            self._narrowest_group_unrailed(),
            self.sidebar_width() if width is None else
            layout_mod.clamp_sidebar_width(width),
        )

    def sidebar_has_something_to_say(
        self, order: "list[str] | None" = None
    ) -> bool:
        """HIDE AT ZERO, the question the ``auto`` setting asks.

        A rail listing one session under no heading is chrome that answers
        nothing -- the same judgment
        :data:`doxa.layout.GROUP_STRIP_MIN_COLS`,
        :data:`doxa.ui.labels.CTX_ABSOLUTE_MIN_COLS` and
        :data:`doxa.diff.SIDE_BY_SIDE_MIN_COLS` each make about their own
        surface. So: any collection at all, or more than one session.

        ``order`` is :meth:`_sidebar_order`'s answer when the caller
        already has it -- see :meth:`refresh_sidebar` on why that list is
        derived exactly once per refresh."""
        if self._collections:
            return True
        known = self._sidebar_order() if order is None else order
        return len(known) > 1

    def sidebar_should_show(self, order: "list[str] | None" = None) -> bool:
        """Should the rail be on screen, before width is considered?"""
        mode = config_mod.sidebar_mode()
        if mode == config_mod.SIDEBAR_OFF:
            return False
        if mode == config_mod.SIDEBAR_ON:
            return True
        return self.sidebar_has_something_to_say(order)

    def _sidebar_order(self) -> "list[str]":
        """Every session the rail knows about, in the order LOOSE ones
        should appear.

        THREE sources, and the third is the design check
        docs/plans/session-sidebar.md asks this feature to answer: mounted
        session panes and archived tabs in strip order, then the sessions
        this window has open but does NOT currently show -- detached
        (Ctrl+W, ``/detach``) and ended (Ctrl+Q) ones, which stay in the
        persisted set and are exactly the peers a user loses track of.

        That third source is what makes the rail a session INDEX rather
        than a second tab strip. A reaped session (``/sessions kill``) is
        not in it: reaping is the one gesture in this app that means
        "forget this conversation", and it means it here too."""
        order: "list[str]" = []
        seen: "set[str]" = set()
        for tab in self._restorable_tabs():
            for session_id in self._tab_session_ids(tab):
                if session_id and session_id not in seen:
                    seen.add(session_id)
                    order.append(session_id)
        for record in (
            *self._detached_this_run.values(), *self._ended_this_run.values()
        ):
            sid = record.session_id
            if sid and sid not in seen and sid not in self._killed_this_run:
                seen.add(sid)
                order.append(sid)
        return [s for s in order if s not in self._killed_this_run]

    def _sidebar_pane(self, session_id: str) -> "Any | None":
        """The mounted surface for a session, of either kind, or
        ``None``."""
        for pane in self.panes():
            if pane._session_id == session_id:
                return pane
        for tab in self.archived_tabs():
            if tab.session_id == session_id:
                return tab
        return None

    def _sidebar_surfaces(self) -> "dict[str, Any]":
        """Every session id on screen, mapped to its surface, in ONE pass.

        :meth:`_sidebar_pane` answers for one id and is the right shape
        for :meth:`reveal_session`, which asks once. A RAIL asks once per
        row, and ``panes()``/``archived_tabs()`` are ``self.query(...)``
        -- a full walk of the screen's widget tree, transcript blocks
        included. Per row that is a walk per session per refresh; this is
        the same answer derived once. Panes win over archived tabs on a
        tie for the same reason :meth:`_sidebar_pane` looks at them
        first: a live session outranks a read-only record of one."""
        surfaces: "dict[str, Any]" = {}
        for pane in self.panes():
            session_id = getattr(pane, "_session_id", "")
            if session_id and session_id not in surfaces:
                surfaces[session_id] = pane
        for tab in self.archived_tabs():
            if tab.session_id and tab.session_id not in surfaces:
                surfaces[tab.session_id] = tab
        return surfaces

    def _sidebar_panes(self) -> "list[triage_mod.PaneEntry]":
        """Every pane GROUP in this window, as the rail's entries.

        **A rail entry is a pane, not a session** (v1.2.0, Part 1b).
        Since v0.97.0 each :class:`doxa.ui.split.PaneGroup` owns its own
        tab strip, so one visible pane can hold three sessions of which
        two are invisible -- and the invisible tab needing input is
        exactly what v1.0.0's flat rail could not surface.

        Read off the widgets, once per refresh, in the same pass
        discipline :meth:`_sidebar_surfaces` states: ``groups()`` and
        ``tabs()`` are queries, and asking them per ROW would be the
        walk-per-session cost v1.0.0 measured at +22% layout time.
        Sessions no group claims -- detached, ended, archived -- are not
        invented here; :func:`doxa.triage.entries_for` gives each its own
        single-member entry, which is the honest reading: there is no
        visible tab because there is no pane."""
        entries: "list[triage_mod.PaneEntry]" = []
        for group in self.groups():
            members: "list[str]" = []
            for tab in group.tabs():
                if not isinstance(tab, (PaneTab, ArchivedSessionTab)):
                    continue
                for session_id in self._tab_session_ids(tab):
                    if session_id and session_id not in members:
                        members.append(session_id)
            if not members:
                continue
            active_tab = group.active_tab()
            active = ""
            if isinstance(active_tab, (PaneTab, ArchivedSessionTab)):
                ids = self._tab_session_ids(active_tab)
                active = ids[0] if ids else ""
            entries.append(
                triage_mod.PaneEntry(
                    group.entry_key, tuple(members), active
                )
            )
        return entries

    def sidebar_rows(
        self, order: "list[str] | None" = None
    ) -> "list[SidebarRow]":
        """The rail's contents, built by the pure function that decides
        what a rail shows -- see :func:`doxa.ui.sidebar.build_rows`."""
        surfaces = self._sidebar_surfaces()
        return build_rows(
            self._collections,
            self._sidebar_order() if order is None else order,
            lambda session_id: self._describe_session(session_id, surfaces),
            width=self.sidebar_width(),
            panes=self._sidebar_panes(),
            collapsed_groups=tuple(self._rail_folded),
        )

    def refresh_sidebar(self, *, force: bool = False) -> None:
        """Re-derive the rail: visibility, width, contents.

        Called from ``_persist_tabset`` (every tab lifecycle event), from
        every collection edit, and from the rail's own ``on_show``.
        ``force`` only bypasses the widget's own "nothing changed" check
        and is what ``on_show`` passes: rows mounted while the rail was
        ``display: none`` were laid out against a zero box."""
        rail = self.sidebar()
        if rail is None:
            return
        # ONE derivation of "which sessions does this window know about"
        # per refresh. _sidebar_order() is self.query(TabPane) -- a full
        # walk of the screen's widget tree -- and this method used to run
        # it twice (through sidebar_should_show, then again through
        # sidebar_rows) with two more walks PER ROW inside
        # _describe_session. Textual's layout and stylesheet passes are
        # synchronous, so DOM work done on a paint path is paid for in
        # event-loop stall (tests/test_split_panes.py measures exactly
        # that), not in microseconds.
        order = self._sidebar_order()
        show = self.sidebar_should_show(order)
        note = self.sidebar_refusal() if show else None
        visible = show and note is None
        rail.set_width(self.sidebar_width())
        rail.styles.display = "block" if visible else "none"
        if not visible:
            # A HIDDEN rail is not built. Not an optimisation for its own
            # sake: this method runs on every tab lifecycle event and on
            # every terminal resize, and on the overwhelmingly common
            # window -- one session, no collections, rail off -- building
            # rows nobody can see would be work done on every keystroke's
            # worth of state change. The rail's own ``on_show`` forces the
            # build back the instant it gets geometry, which is the same
            # remembered-intent shape ``SessionPane.scroll_transcript_to_end``
            # uses for the same measured reason (v0.99.0): a hidden widget
            # has no geometry, so the work has to wait for the show.
            rail.set_rows([])
            return
        if force:
            rail._rows = []
        rail.set_rows(self.sidebar_rows(order))

    def refresh_sidebar_marks(self, pane: "Any") -> None:
        """One pane's marks moved. Update that ROW rather than the rail.

        Called from ``SessionPane._set_tab_class``, which is also what
        drives the needs-input blink -- at 2 Hz, per waiting session. A
        rebuild there would be a repaint of the whole rail per blink,
        which is the busy-idle cost this app measures and refuses. If the
        row is not there at all the structure moved, and the full refresh
        is the right answer once.

        **A rail that is not showing is not touched at all**, and that is
        the load-bearing half. ``display: none`` means the rail holds no
        rows (``refresh_sidebar`` empties it), so ``apply_marks`` could
        never find one and the fallback below fired on EVERY mark toggle
        -- ``-working`` on and off per turn, ``-done-unseen``,
        ``-staged``, ``-attention`` per blink -- in every window in the
        app, which is the overwhelmingly common one because the rail is
        hidden by default. Each of those ran the whole derivation. A mark
        moving is by definition NOT a structure change: the structure is
        refreshed by ``_persist_tabset`` on every tab lifecycle event, by
        every collection edit, and by the rail's own ``on_show`` the
        instant it gets geometry."""
        rail = self.sidebar()
        if rail is None or rail.styles.display == "none":
            return
        session_id = getattr(pane, "_session_id", "") or ""
        if not session_id:
            return
        marks = tuple(
            name for name in labels_mod.TAB_STATE_MARKS
            if labels_mod.mark_over([pane], name)
        )
        if not rail.apply_marks(session_id, marks, self._pane_ctx(pane)):
            self.refresh_sidebar()

    def set_sidebar(self, visible: bool) -> "str | None":
        """Show or hide the rail, and WRITE that choice. Returns the one
        line the user is told, or ``None``.

        The write is what ends hide-at-zero's guessing (see
        :func:`doxa.config.sidebar_mode`): a user who closed the rail must
        not have it reappear because they opened a second tab.

        A refusal does NOT write. The rail could not open at this width,
        which is a fact about the terminal and not a decision about the
        rail -- recording it as one would leave the user's next, wider
        window without the sidebar they asked for."""
        if visible:
            note = self.sidebar_refusal()
            if note:
                return note
        with contextlib.suppress(Exception):
            config_mod.save({"sidebar": "1" if visible else "0"})
        config_mod.invalidate()
        self.refresh_sidebar(force=True)
        return None

    def notify_sidebar(self, note: str) -> None:
        """Put one rail message in front of the user. The active pane's
        transcript when there is one, Textual's own notification
        otherwise (a window whose only tab is an archive has no
        transcript to write into)."""
        pane = self.active_pane
        if pane is not None:
            pane.run_worker(pane._system(note), group="sidebar-note")
            return
        with contextlib.suppress(Exception):
            self.notify(note)

    def reveal_session(self, session_id: str) -> "str | None":
        """Take me to that session: focus its group, activate its tab.

        Returns the line to show when it cannot -- and "cannot" is a real
        answer here, not a defect. The rail lists sessions this window
        knows about, including ones that are not mounted in any group, so
        a row can genuinely name a session there is nowhere to go TO. It
        says so and names the door back in (``/attach``) rather than
        pretending a click did something."""
        if not session_id:
            return None
        surface = self._sidebar_pane(session_id)
        if isinstance(surface, SessionPane):
            # _switch_to_tab does the three beats every explicit switch in
            # this file does -- activate, move the marker, focus -- and
            # focusing a pane puts the keyboard in its GROUP, which is what
            # "focus its group" means since v0.97.0.
            self._switch_to_tab(surface.id or "")
            return None
        if surface is not None:  # an archived tab: activate it, focus its scroll
            self._activate_tab(surface)
            self._jump_tab_marker()
            self._focus_tab(surface)
            return None
        return (
            f"{session_id[:8]} is not open in this window — "
            f"/attach {session_id[:8]} brings it back in a new tab"
        )

    def focus_group_by_key(self, entry_key: str) -> bool:
        """Put the keyboard in the group with that ``entry_key``, WITHOUT
        touching which of its tabs is active. Returns whether there was
        such a group.

        The distinction is the whole of option C's heading gesture. A
        group's heading summarises every tab it holds, including the ones
        that are not on screen, so a click on it that ALSO switched the
        active tab would be the rail changing what you are looking at as
        a side effect of asking about it -- and there would then be no
        gesture left that means "go there and leave it alone".

        Focusing the group's ACTIVE tab is what "focus the group" means
        since v0.97.0, and focusing a widget inside the tab that is
        already active cannot activate a different one."""
        for group in self.groups():
            if group.entry_key != entry_key:
                continue
            surface = next(iter(group.surfaces()), None)
            if surface is None:
                # The group is in the DOM but has nothing PAINTED -- mid
                # teardown, or not laid out yet. "There is no such group"
                # is the wrong answer and "I focused it" is a lie, so
                # this reports the only true one and lets the caller fall
                # back to revealing the session by itself.
                return False
            self._focus_tab(surface)
            return True
        return False

    def toggle_group_expanded(self, entry_key: str) -> None:
        """Fold or unfold one pane group's tab rows, and REMEMBER it.

        Persisted in the tabset record beside the collapsed flag a
        collection already has (:mod:`doxa.tabsets`), because a fold is a
        statement about how the user wants to read this window and a
        window that forgot it on every restart would be asking them to
        make it again."""
        if not entry_key:
            return
        if entry_key in self._rail_folded:
            self._rail_folded.discard(entry_key)
        else:
            self._rail_folded.add(entry_key)
        self.refresh_sidebar(force=True)
        self._persist_tabset()

    def resize_sidebar(
        self, width: int, *, persist: bool = True
    ) -> "str | None":
        """Set the rail's width. Returns the refusal, or ``None``.

        **The same floor opening refuses at**, asked through the same
        :meth:`sidebar_refusal` -- see that method. ``persist`` writes it
        to the settings registry, which the drag defers to its last event
        and the keys do on every press."""
        want = layout_mod.clamp_sidebar_width(width)
        note = self.sidebar_refusal(want)
        if note:
            return note
        # The override is spent only when the write actually LANDED. A
        # failed save (read-only home, a full disk) costs the user the
        # next launch's width and nothing else -- but clearing the
        # override anyway would leave the painted rail at ``want`` while
        # ``sidebar_width()`` answered with the value still on disk, and
        # the next refresh would snap the rail back under the user's
        # hand. Chrome never costs a session, and it does not get to
        # disagree with itself either.
        kept = want
        if persist:
            try:
                config_mod.save({"sidebar_width": str(want)})
            except Exception:  # noqa: BLE001 -- chrome never costs a session
                pass
            else:
                kept = None
            config_mod.invalidate()
        self._sidebar_width_override = kept
        rail = self.sidebar()
        if rail is not None:
            rail.set_width(want)
        return None

    def _nudge_sidebar(self, step: int) -> None:
        """``Alt+Shift+←/→``: the rail divider from the KEYBOARD.

        A mouse-only divider is unreachable for a keyboard user, and it is
        also unreachable over an ssh session to a terminal with no mouse
        reporting -- this project has ruled on that twice, and the two
        dividers it already has (``Ctrl+↑/↓`` in-pane, ``Alt+arrow`` for a
        leaf) are both keyboard gestures first.

        Refusals are REPORTED here and swallowed in the drag: one press is
        one statement, and a user who pressed a key and saw nothing happen
        is owed the reason (the v0.39.0 rule about a documented key that
        silently does nothing)."""
        rail = self.sidebar()
        if rail is None or rail.styles.display == "none":
            self.notify_sidebar(
                "the session sidebar is hidden — F3 or /sidebar opens it"
            )
            return
        note = self.resize_sidebar(self.sidebar_width() + step)
        if note:
            self.notify_sidebar(note)

    def _apply_collections(
        self, result: "tuple[Any, str | None]"
    ) -> "str | None":
        items, note = result
        if note is None:
            self._collections = items
            self.refresh_sidebar(force=True)
            self._persist_tabset()
        return note

    def collection_new(self, name: str) -> "str | None":
        return self._apply_collections(
            collections_mod.new(self._collections, name)
        )

    def collection_rename(self, old: str, new_name: str) -> "str | None":
        return self._apply_collections(
            collections_mod.rename(self._collections, old, new_name)
        )

    def collection_delete(self, name: str) -> "str | None":
        return self._apply_collections(
            collections_mod.delete(self._collections, name)
        )

    def collection_assign(self, name: str, session_id: str) -> "str | None":
        return self._apply_collections(
            collections_mod.assign(self._collections, name, session_id)
        )

    def collection_unassign(self, session_id: str) -> "str | None":
        return self._apply_collections(
            collections_mod.unassign(self._collections, session_id)
        )

    def collections(self) -> "tuple[collections_mod.Collection, ...]":
        """The window's collections. A tuple of frozen records, so a
        caller reading them cannot edit them by accident -- every edit
        goes through the five methods above."""
        return self._collections

    def _flash_group_numbers(self) -> None:
        """Paint each group's own number over its own region, briefly.

        **Nothing at all when there is only one group**: there is no choice
        to make, so there is nothing to teach. Hide-at-zero, as everywhere
        else in this app.

        **One-shot, never an interval.** DOXA has a no-timer rule and its
        target is IDLE CPU -- v0.78.0 already amended it for the turn
        spinner on the grounds that a timer existing only during a turn
        spends nothing. A ``set_timer`` armed by a keystroke and fired once
        is the same bargain: no interval, nothing running while idle, and
        the previous one is cancelled before a new one is armed so a held
        key cannot stack them.

        Drawn per group from the same rectangles the numbering is derived
        from, so what is numbered and what is painted cannot disagree."""
        self._cancel_group_flash()
        groups = self._group_order()
        if len(groups) < 2:
            return
        for index, group in enumerate(groups, start=1):
            group.show_number(index)
        with contextlib.suppress(Exception):
            self._group_flash_timer = self.set_timer(
                self.GROUP_FLASH_SECS, self._hide_group_numbers
            )

    def _cancel_group_flash(self) -> None:
        timer, self._group_flash_timer = self._group_flash_timer, None
        if timer is not None:
            with contextlib.suppress(Exception):
                timer.stop()

    def _hide_group_numbers(self) -> None:
        self._group_flash_timer = None
        for group in self.groups():
            group.hide_number()

    def _dismiss_group_numbers(self, event: "events.Key") -> None:
        """Any subsequent key takes the overlay away at once.

        The spec's own instruction ("cancelled on the next key"), and the
        reason the flash never outstays a user who is already moving. A
        ``Ctrl+<digit>`` is exempt because it is the key that arms one --
        Textual delivers the key event and runs the action from the same
        press, and without this exemption the flash would cancel itself."""
        if self._group_flash_timer is None:
            return
        key = event.key or ""
        if key.startswith("ctrl+") and key[-1].isdigit():
            return
        self._cancel_group_flash()
        self._hide_group_numbers()

    def focus_group_number(self, number: int) -> "str | None":
        """``/pane <n>`` -- the door that always works, for the terminals
        where ``Ctrl+<digit>`` produces no byte at all. Returns a refusal
        to show the user, or ``None`` when it happened."""
        groups = self._group_order()
        if len(groups) < 2:
            return "there is only one pane group — nothing to jump to"
        if not (1 <= number <= len(groups)):
            return (
                f"there is no pane group {number} — this window has "
                f"{len(groups)}, numbered left to right then top to bottom"
            )
        self.action_focus_group(number)
        return None

    async def move_tab_to_group(self, number: int) -> "str | None":
        """``/movepane <n>`` -- take the focused group's ACTIVE tab and put
        it in the group at that position. Returns a refusal, or ``None``.

        **This is the constraint the whole design turns on.** Textual 5.3
        cannot re-parent a mounted widget: ``mount`` of an already-mounted
        widget is a silent no-op that ORPHANS it (measured in v0.91.0, not
        assumed). So this does NOT move the tab. It builds a NEW tab and a
        NEW surface at the destination, hands the new surface the SESSION
        the old one was driving, and tears the old tab down.

        The session survives untouched because the session does not live in
        the widget: it lives in the daemon, behind an engine handle
        (``doxa.client.EngineClient``, or an in-process ``SessionEngine``),
        and ``SessionPane.adopt`` is what re-seats that handle. The pane is
        a VIEW of a session, and this is the first gesture in DOXA that
        makes the difference load-bearing rather than academic.

        Refused, with no change at all, when there is nowhere to move to,
        when the destination is where the tab already is, or when the tab
        is the only one in a group that would then have to close -- moving
        the last tab OUT of a group is a close and a move at once, and the
        two have different undo stories."""
        groups = self._group_order()
        if len(groups) < 2:
            return "there is only one pane group — nothing to move a tab to"
        if not (1 <= number <= len(groups)):
            return (
                f"there is no pane group {number} — this window has "
                f"{len(groups)}, numbered left to right then top to bottom"
            )
        source = self.focused_group()
        target = groups[number - 1]
        if source is None:
            return "there is no pane group here to move a tab out of"
        if source is target:
            return f"this tab is already in pane group {number}"
        tab = source.active_tab()
        if tab is None:
            return "there is no tab here to move"
        # SESSION tabs only, and the guard is on the METHOD rather than on
        # the result: an archived tab and a subagent transcript are both
        # ``TabPane``s in this strip and neither has ``leaves()`` at all,
        # so asking one would be an AttributeError rather than a refusal.
        leaves = getattr(tab, "leaves", None)
        pane = next(iter(leaves()), None) if callable(leaves) else None
        if pane is None:
            return (
                "only a session tab can be moved between groups today — "
                "a diff belongs beside the session it is a diff of, and a "
                "read-only tab has nothing to re-seat"
            )
        if len(source.tabs()) < 2:
            return (
                f"this is pane group {groups.index(source) + 1}'s last tab — "
                "moving it would close the group. close it with Ctrl+W, or "
                "split the destination instead"
            )
        return await self._reseat_pane(pane, target)

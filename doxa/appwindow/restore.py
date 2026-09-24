# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.appwindow.restore -- the tab set, written down and put back.

One window's tabs survive the process that drew them: item D persists the
whole set to ``$DOXA_HOME/tabsets/<scope>.json`` on every change that could
alter it, and the next launch composes the window back out of that record
rather than out of one fresh pane. This module is both halves of that round
trip, plus the tab construction they share.

Going out: :meth:`WindowRestoreMixin._restorable_tabs` says which tabs the
record is about and in which order, :meth:`_tab_session_ids` says which
sessions each of them contributes, :meth:`_activation_pending` tells "Textual
has not picked an active tab yet" apart from "the active tab is not a
session", :meth:`_note_pane_booted` waits until a restore's panes have all
reported their session ids, and :meth:`_persist_tabset` merges the mounted
panes with the ones detached this run and writes the file.

Coming back: :meth:`_restore_group_tree` prunes the saved layout to the specs
that actually reattached, :meth:`_restored_pane` wires one live leaf,
:meth:`_compose_restored_root` builds the whole window out of them,
:meth:`_initial_active_tab_id` decides which tab each group opens on before
anything is mounted, and :meth:`_activate_initial_tab` puts the keyboard on
it afterwards. :meth:`_make_tab` and :meth:`_pinned_transcripts` serve both
directions -- the first wraps every pane this app ever shows, the second
names the transcripts a relayout has to keep standing on their newest block.

``DoxaApp.compose`` stays in :mod:`doxa.app`. It is the window's visible
surface and the one method here that Textual itself calls; what it does with
a restore is one line into :meth:`_compose_restored_root`.

The methods move verbatim -- same order, same docstrings, same comments --
as the second of DoxaApp's method families to become a mixin, after
:mod:`doxa.appwindow.failures`. Not one of the twelve carries a decorator
Textual reads: two are ``@staticmethod``, which is a plain descriptor, and an
``@on`` handler would have had to stay behind (``_MessagePumpMeta`` only ever
scans the class body it builds). Half of them sat under DoxaApp's
``-- item D: persisted tab set`` banner, which stays in doxa/app.py because
``_window_root`` is still under it.
"""

from __future__ import annotations

import contextlib
from typing import Any

from textual.widgets import Static, TabPane
from textual.css.query import NoMatches

from .. import collections as collections_mod
from .. import layout as layout_mod
from .. import peers as peers_mod
from .. import tabsets as tabsets_mod
from ..session.pane import SessionPane
from ..ui import split as split_mod
from ..ui.diffview import DiffPane
from ..ui.split import PaneGroup, PaneTab
from ..ui.transcript import _restore_pane_id, ArchivedSessionTab, RestoreTabSpec


class WindowRestoreMixin:
    """DoxaApp's tab-set half. Mixed into the app, never used standalone:
    every method here reads window state through ``self`` -- the mounted
    groups and panes, the specs a launch was given to restore, and the
    records of what was detached or ended this run."""

    @staticmethod
    def _pinned_transcripts(group: "PaneGroup") -> "list[Any]":
        """Every leaf in ``group`` -- across ALL of its tabs, not only the
        one it is showing -- whose transcript is standing on its newest
        block.

        Duck-typed twice over, the way every walker in this file is: a
        group holds ``PaneTab``s beside ``ArchivedSessionTab``s and
        subagent transcripts, and only the first has ``leaves()``, only a
        ``SessionPane`` has ``transcript_at_end()``. Anything that answers
        neither has no tail to keep."""
        pinned: "list[Any]" = []
        for tab in group.tabs():
            leaves = getattr(tab, "leaves", None)
            if not callable(leaves):
                continue
            for leaf in leaves():
                at_end = getattr(leaf, "transcript_at_end", None)
                if callable(at_end) and at_end():
                    pinned.append(leaf)
        return pinned

    def _make_tab(self, pane: "SessionPane", *, id: "str | None" = None) -> PaneTab:
        """Wrap one pane in the tab that holds it.

        Every tab in this app is a :class:`~doxa.ui.split.PaneTab` holding
        exactly one surface -- which is what a tab was through v0.88.0 and
        is what it is again since v0.97.0 moved the layout tree up to the
        window. What a later split is built INTO is the empty
        :class:`~doxa.ui.split.SplitBox` chain around the GROUP now; see
        that module's docstring for why it cannot be created on demand.

        The TAB takes the id its pane used to carry, so
        :func:`_restore_pane_id`, :data:`_FALLBACK_PANE_ID`,
        :meth:`_initial_active_tab_id` and every ``tabbed.active =``
        assignment in this file keep naming the same strings."""
        tab = PaneTab(
            pane.born_title, pane,
            # Distinct from the leaf's own id, deliberately. Textual only
            # forbids duplicate ids among SIBLINGS, so a tab and the pane
            # inside it could legally share one -- and every id-selector
            # query in this app would then resolve to whichever the
            # breadth-first walk reached first, which is the tab, silently,
            # for the rest of the release. Derived from the pane's id so a
            # DOM dump still reads as a pair.
            id=id or f"tab-{pane.id}",
        )
        tab.focused_leaf = pane
        return tab

    def _restorable_tabs(self) -> "list[Any]":
        """Every tab the persisted set is about, IN STRIP ORDER -- session
        panes and archived tabs interleaved exactly as the user sees them,
        because the record's order IS the tab-bar order it will restore
        to. Subagent transcript tabs are not sessions and never appear.

        Groups in LAYOUT order and, within each, strip order -- which is
        what a plain DOM walk gives, because the owner-first invariant
        makes a ``Split``'s children left-to-right / top-to-bottom by
        construction. Deliberately not :meth:`_group_order`'s painted
        reading order: this feeds the flat ``tabs`` list, whose companion
        is the tree written beside it, and the two must agree about order
        or a record disagrees with itself."""
        return [
            tab for tab in self.query(TabPane)
            if isinstance(tab, (PaneTab, ArchivedSessionTab))
        ]

    def _activation_pending(self) -> bool:
        """Has Textual decided WHICH tab is active yet?

        ``TabbedContent.active`` is a reactive that starts as the empty
        string and is only filled in when the inner ``Tabs`` widget's own
        mount handler picks a tab and its watcher posts ``TabActivated``
        -- several message-pump turns after the panes themselves exist and
        can already be running. ``active_pane`` is None for that whole
        window, which is a DIFFERENT statement from "a tab that is not a
        session is active": one is "not yet", the other is an answer.
        :meth:`_persist_tabset` is the caller that has to tell them
        apart."""
        try:
            return not self._strip().active
        except Exception:
            return True

    def _note_pane_booted(self, pane: "SessionPane") -> None:
        """A pane's session id is stable now (first boot), or has just
        CHANGED (switch_engine -- a fresh /model session or a palette
        attach landing in this same tab). Either way the persisted set
        needs to know -- except mid-restore, where every restored pane
        boots concurrently and each one's session_id becomes known at an
        unpredictable moment: persisting after the FIRST to finish would
        write a truncated set missing every tab still connecting. This
        counts restored panes down and only calls through once all of
        them (if any) have reported in, so the very first write already
        reflects the complete restored set."""
        if self._restore_pending > 0:
            self._restore_pending -= 1
            if self._restore_pending > 0:
                return
        self._persist_tabset()

    def _note_pane_startup_finished(self, pane: "SessionPane") -> None:
        """Reveal the window once every opening pane has reached a result."""
        if pane not in self._startup_waiting_panes:
            return
        self._startup_waiting_panes.remove(pane)
        self._startup_pending = len(self._startup_waiting_panes)
        if self._startup_pending == 0:
            # A very fast boot can finish before this widget is composed.
            # DoxaApp.on_mount applies the same state once it exists.
            with contextlib.suppress(NoMatches):
                self.query_one("#startup-status", Static).display = False

    def _persist_tabset(self, *, exclude_session_id: "str | None" = None) -> None:
        """Snapshot the CURRENT tab set to $DOXA_HOME/tabsets/<scope>.json
        (doxa.tabsets.save) -- called on every tab-set change (open,
        rename, close-detach, close-stop, app exit). Unconditional on the
        restore_tabs SETTING (that only gates whether a later launch
        READS this file, see doxa.tabsets.enabled/config's own note) --
        gated only on a restore still being in flight (_restore_pending).

        TWO sources, merged: panes still mounted (in tab-bar order, LIVE
        only -- a _stopped one is skipped, see below) and
        _detached_this_run (sessions Ctrl+W'd out of the strip earlier
        this run, which keep running and therefore STAY in the set per
        item D #4). _ended_this_run (Ctrl+Q, palette-stopped) is
        deliberately NOT a source here -- see that dict's own docstring.
        v0.55.0 dropped a _stopped mounted pane on the spot, because
        ending a session really did mean losing the tab for good. v0.56.0
        pinned the doxa session id to the CLI's own
        (SessionEngine._build_options), which is what makes --resume able
        to replay a transcript DOXA itself indexed, and v0.60.0 read that
        as license to stop excluding a _stopped pane here at all -- "the
        daemon is gone" no longer meant "the tab is gone", so why should
        ending a session cost the user the tab. It still can: v0.60.0
        never noticed that a pinned-id resume plays back LIVE, not
        read-only (finalize() never touches the CLI's own history store),
        so a session the user explicitly ended with Ctrl+Q came back next
        launch exactly as if it had never closed. v0.99.1 restores the
        v0.55.0 exclusion -- a mounted _stopped pane is skipped here again
        -- which is the one piece of this method that changed; the
        _resume-a-dead-session-as-archived_ path (doxa.cli.ended_tab_spec)
        this reverses nothing about, because that path is only ever
        reached for a session that IS still in the persisted set (a
        Ctrl+W detach whose daemon later died on its own), which is
        exactly the case v0.55.0 never touched either. The one thing that
        still has to win over both sources is an EXPLICIT reap
        (_killed_this_run, `/sessions kill`) -- checked below wherever a
        record could otherwise slip through.

        Cross-repo exclusion (item 4's repo picker, reconciled against
        this method): every tab used to share ONE scope by construction
        (Ctrl+T only ever spawned in THIS app's own cwd) -- the repo
        picker's "open in a new tab" is the first way a single window
        can host a tab rooted in a DIFFERENT repo. Such a pane's own
        session is scoped elsewhere already (its daemon's PeerHost wrote
        ITS OWN registry entry under ITS OWN scope key), so writing its
        id into THIS window's tabset file would be dead weight at best --
        doxa.tabsets.resolve cross-checks a saved id against
        list_daemons(scope_key=<this file's own scope>), and a daemon
        registered under a DIFFERENT scope key is invisible to that
        check, so the entry could only ever resolve to "gone" and get
        silently skipped, never to the wrong session. Excluded here
        rather than relying on that safe-but-wasteful fallback.

        ``exclude_session_id`` (v0.85.0): one more session to leave out of
        every source above, for exactly one call -- :meth:`_close_pane`'s
        own ``is_last`` branch, which needs THIS snapshot to read as
        though the closing tab were already gone without actually
        unmounting it first. Still load-bearing for a Ctrl+W is_last close
        (the pane is only DETACHED, never marked _stopped, so nothing else
        here would exclude it); redundant but harmless for a Ctrl+Q
        is_last close since v0.99.1 -- pane.stop() already marked it
        _stopped by the time this runs, so the mounted-pane scan's own
        exclusion above would have caught it anyway. An earlier version of
        that fix called
        ``remove_pane`` before this method to get the same exclusion out
        of the mounted-pane scan -- which worked, but unmounted a pane
        with a still-running ``_peer_pump`` worker moments before
        ``action_quit`` tore the app down under it, a teardown race
        (measured as an intermittent ``AssertionError`` out of
        ``SessionPane._peer_pump``'s own ``assert self.engine is not
        None``, surfaced as a visible in-app error block on the way out)
        that plain exclusion does not create: the pane stays mounted,
        exactly as unconditional ``App.action_quit`` already handled it
        pre-v0.85.0, and only what gets WRITTEN changes."""
        # ABOVE the restore guard, and above the write itself: every tab
        # lifecycle event passes through this method (see the docstring),
        # which makes it the one honest hook for "some group's tab count
        # moved" -- and a restore still in flight is precisely when a
        # group is gaining the tabs that decide whether it shows a strip.
        # Nothing here writes to disk, so the guard has nothing to protect
        # against.
        self.refresh_strip_visibility()
        if self._restore_pending > 0:
            return
        scope = peers_mod.main_repo_root_of(self.cwd) or self.cwd
        active_tab = self._active_tab()
        # WHICH LEAF is the active one, asked in the order that is true
        # synchronously. ``Widget.focus()`` is deferred in Textual 5.3 (it
        # schedules ``screen.set_focus`` with ``call_later``), so right
        # after Ctrl+T -- which activates the new tab and focuses its leaf
        # in the same handler, then persists -- ``active_pane`` still reads
        # the pane the user came FROM. ``PaneTab.focused_leaf`` is written
        # by ``_focus_tab`` synchronously, so it is the answer that is
        # already correct at this instant; ``active_pane`` is the fallback
        # for a tab nobody has focused into yet. Getting this backwards
        # saved the wrong active session -- the same class of defect as
        # v0.38.0's null active id, with a wrong value instead of a
        # missing one.
        active_leaf = None
        if isinstance(active_tab, PaneTab):
            leaf = active_tab.focused_leaf
            if isinstance(leaf, SessionPane) and leaf.tab is active_tab:
                active_leaf = leaf
            else:
                active_leaf = self.active_pane
        tabs: "list[tabsets_mod.TabRecord]" = []
        seen: set[str] = set()
        active_id: "str | None" = None
        # Tab-strip order within a group, groups in layout order, and BOTH
        # kinds of restorable tab: a live PaneTab and (v0.32.0) an
        # ArchivedSessionTab, which is one of the user's open tabs too and
        # must not evaporate on the next restart just because the session
        # behind it already has.
        for tab in self._restorable_tabs():
            if isinstance(tab, ArchivedSessionTab):
                if tab.session_id in seen or tab.session_id == exclude_session_id:
                    continue
                seen.add(tab.session_id)
                tabs.append(tab.as_record())
                if tab is active_tab:
                    active_id = tab.session_id
                continue
            for pane in tab.leaves():
                sid = pane._session_id
                if (
                    not sid or sid in seen or sid in self._killed_this_run
                    or sid == exclude_session_id or pane._stopped
                ):
                    continue
                # A _stopped pane (Ctrl+Q, palette stop) is excluded here
                # again as of v0.99.1 -- see this method's own docstring
                # for the v0.60.0 detour and why it did not hold. A pane
                # can still be MOUNTED and _stopped at once (pane.stop()
                # marks the flag and clears the engine handle, but nothing
                # unmounts the pane itself until _close_pane's caller gets
                # around to it, deliberately -- see is_last's own
                # comment), so this is reached mid-close, not just at
                # startup.
                pane_cwd = str(getattr(pane.engine, "cwd", None) or pane.cwd)
                pane_scope = peers_mod.main_repo_root_of(pane_cwd) or pane_cwd
                if pane_scope != scope:
                    continue
                seen.add(sid)
                tabs.append(tabsets_mod.TabRecord(sid, pane.custom_name, pane_cwd))
                if pane is active_leaf or (
                    active_leaf is None and tab is active_tab
                ):
                    active_id = sid  # noqa: E501 -- see active_leaf above
        if (
            active_id is None
            and self._restore_active_id is not None
            and self._restore_active_id in seen
            and self._activation_pending()
        ):
            # The write-ordering race, fixed in v0.38.0. A restore's FIRST
            # write is triggered by the last restored pane reporting its
            # session id (_note_pane_booted), and a pane can boot before
            # Textual has resolved which tab is active: TabbedContent.
            # active is still the empty string it starts as, active_pane
            # is therefore None, no tab matches `is active_tab`, and
            # active_id would be saved as null. Nothing writes again until
            # the tab set next changes, so that one racy write is what
            # lands on disk -- the tabs restore complete and in order, on
            # the WRONG tab, silently. Measured as 1 failure in 80 runs of
            # tests/test_tabsets.py's restore test with four suites in
            # parallel; the signature is a null active id, never a wrong
            # one.
            #
            # In exactly that window the record we restored FROM is the
            # answer, and it cannot be stale: no tab is active yet, so the
            # user cannot have switched away from one. The
            # _activation_pending() guard is what keeps this from firing
            # later, when a None active_id is a real answer -- a subagent
            # transcript tab is active, and no session tab is.
            active_id = self._restore_active_id
        # _detached_this_run ONLY (v0.99.1 -- _ended_this_run used to sit
        # here too; it no longer does, see that dict's own docstring): a
        # session whose tab already left the strip is in this flat dict
        # and nowhere else -- there is no layout left to remember for it,
        # and inventing one would put it back somewhere the user never had
        # it. _fill_group appends it to the first group at restore time,
        # which is "it comes back as a tab", the same answer v0.91.0 gave
        # with a single-leaf tree.
        for record in self._detached_this_run.values():
            if (
                record.session_id in seen
                or record.session_id in self._killed_this_run
                or record.session_id == exclude_session_id
            ):
                continue
            seen.add(record.session_id)
            tabs.append(record)
        # The WINDOW's layout, read off the widgets and then PRUNED to the
        # sessions that actually made it into the flat list above (a
        # cross-repo pane, a reaped one, the excluded last tab). A tree
        # that still named them would restore a pane-shaped hole; the
        # survivors take the space proportionally instead.
        groups = split_mod.tree_of(self._window_root())
        kept = [record.session_id for record in tabs]
        if groups is not None:
            groups = layout_mod.prune(groups, kept)
        # v1.0.0: the COLLECTIONS ride along, pruned to the same flat list
        # the tree is pruned to, so the three halves of the record agree
        # about which sessions exist. Pruned on the app's own copy too, not
        # only in the record -- a collection that kept naming a reaped
        # session would put a dead row back on the rail the moment
        # something else caused a refresh.
        self._collections = collections_mod.prune(self._collections, kept)
        with contextlib.suppress(Exception):
            tabsets_mod.save(
                scope, tabs, active_id, groups=groups,
                collections=self._collections,
                rail_folded=tuple(self._rail_folded),
            )
        # The rail is a view of exactly this snapshot, so the one method
        # that runs on every tab lifecycle event is the one place it needs
        # refreshing from. Suppressed and last: a persistence path must
        # never be the thing that fails because chrome could not repaint.
        with contextlib.suppress(Exception):
            self.refresh_sidebar()

    @staticmethod
    def _tab_session_ids(tab: "Any") -> "list[str]":
        """The session ids one restorable tab contributes. A ``PaneTab``
        answers through its leaves (a diff tab has none, correctly); an
        ``ArchivedSessionTab`` carries its own."""
        if isinstance(tab, ArchivedSessionTab):
            return [tab.session_id]
        return [
            leaf._session_id
            for leaf in getattr(tab, "leaves", list)()
            if getattr(leaf, "_session_id", "")
        ]

    def _restored_pane(self, spec: "RestoreTabSpec", leaf: "Any" = None) -> SessionPane:
        """One restored LIVE leaf, from the spec doxa.cli resolved and
        (v0.91.0) the layout leaf that says where in its tab it sits.

        Extracted from :meth:`compose` when a tab stopped being one pane:
        a split tab builds several of these, and every one of them needs
        the identical restore wiring -- the pinned name applied before
        boot, the resume-vs-reattach choice about where the scrollback
        comes from, the saved cwd, and (v0.91.0) the saved position of the
        pane's own status-bar divider."""
        pane = SessionPane(
            self._tab_title(), self.cwd, self.model,
            spec.engine_factory,
            # The TAB keeps ``restore-<session id>`` -- that is the string
            # _initial_active_tab_id, ``tabbed.active`` and the persisted
            # record's own lookups all name. The LEAF inside it needs an id
            # of its own now that the two are different widgets, and it is
            # derived from the same one so a DOM dump still reads.
            id=f"{_restore_pane_id(spec.session_id)}-leaf",
        )
        if spec.pinned_name:
            pane._initial_pinned_name = spec.pinned_name
        if spec.resume:
            # v0.56.0: this tab's session had ENDED, and it is coming back
            # LIVE, continuing that conversation (doxa.cli decided that;
            # the engine_factory above spawns with --resume). Its
            # scrollback comes from the same transcript file a reattach
            # reads, minus the backlog-skip precondition -- a freshly
            # spawned daemon has no ring to replay on top. See
            # SessionPane._restore_transcript.
            pane._resume_from = spec.session_id
        else:
            # v0.32.0: this pane's scrollback comes from the session's
            # persisted transcript, not the daemon's 512-frame ring (see
            # SessionPane._restore_transcript).
            pane._restore_transcript_wanted = True
        pane._restore_cwd = spec.cwd
        if leaf is not None:
            pane.prompt_ratio = layout_mod.clamp_prompt_ratio(leaf.prompt_ratio)
        self._startup_waiting_panes.add(pane)
        return pane

    def _restore_group_tree(self) -> "layout_mod.Node | None":
        """The WINDOW's layout tree for this restore, pruned to the specs
        that actually came back and guaranteed to place every one of them.

        Answers for all three record eras by delegating to the ONE reader
        that knows them -- :func:`doxa.tabsets._fill_group`, the same
        function :func:`doxa.tabsets._layout_groups` ends every branch with
        -- so a launch through ``doxa.cli`` (which passes ``restore_groups``
        straight through) and a launch through a hand-built ``DoxaApp``
        (which may pass only the older ``restore_layout``) cannot disagree
        about what a saved record means.

        The pruning is what makes a restore honest: the saved tree names
        sessions, and by the time this runs some of them are dead. A tree
        that still named them would restore a region with nothing in it."""
        specs = self._restore_tabs
        if not specs:
            return None
        records = [
            tabsets_mod.TabRecord(s.session_id, s.pinned_name, s.cwd)
            for s in specs
        ]
        tree = self._restore_groups
        if tree is None and self._restore_layout:
            # A caller that only had the v0.91.0 shape. The composition
            # rule is doxa.tabsets' -- the ACTIVE tab's tree is the window,
            # the rest become its tabs -- restated here only as the choice
            # of WHICH tree, because that module's copy reads a raw record
            # and this one has already-parsed trees in hand.
            chosen = self._restore_layout[0]
            if self._restore_active_id:
                for candidate in self._restore_layout:
                    ids = {
                        leaf.session_id
                        for leaf in layout_mod.leaves(candidate)
                    }
                    if self._restore_active_id in ids:
                        chosen = candidate
                        break
            tree = layout_mod.groupify(chosen)
        alive = {s.session_id for s in specs}
        pruned = layout_mod.prune(tree, alive) if tree is not None else None
        return tabsets_mod._fill_group(pruned, records, self._restore_active_id)

    def _compose_restored_root(self) -> "Any":
        """The restored window: one tree of groups, each holding its own
        tabs, in saved order throughout.

        v0.32.0 mixes two kinds in that order -- a live spec reattaches its
        daemon (``SessionPane``), an archived one has no daemon left and
        renders its transcript read-only (``ArchivedSessionTab``) --
        v0.92.0 adds a third (a ``DiffPane`` tab, restored as a diff with
        nothing to reattach), and v0.97.0 adds no kind at all: it only
        changes which container they land in.

        No pane arms a mount-time focus (v0.38.0): a restored pane mounts
        in the BACKGROUND, and which group ends up focused is decided once,
        explicitly, in :meth:`_activate_initial_tab`. v0.23.0's "three
        restored tabs always land on the last one" defect was that same
        entanglement.

        The report block (if any) rides on the first LIVE pane -- an
        archived tab already opens with a block of its own explaining what
        it is."""
        specs = {s.session_id: s for s in self._restore_tabs}
        tree = self._restore_group_tree()
        placed: "set[str]" = set()
        first_pane: "list[SessionPane]" = []

        def _tab_for(leaf: "layout_mod.Leaf") -> "Any":
            spec = specs.get(leaf.session_id)
            if leaf.is_diff:
                # v0.92.0: the diff restores as a diff, with no session
                # behind it and nothing to reattach -- it re-reads
                # `git diff` on mount. A QUEUED-but-unapplied rejection
                # does NOT survive, because it is held on the widget and
                # the widget is new; it is discarded WITH the pane, and
                # the pane comes back showing the un-reverted hunk, which
                # is the truth.
                surface = DiffPane(
                    leaf.session_id,
                    leaf.cwd or (spec.cwd if spec else None) or self.cwd,
                    id=f"{_restore_pane_id(leaf.session_id)}-diff",
                )
                return PaneTab(
                    self._tab_title(), surface,
                    id=f"{_restore_pane_id(leaf.session_id)}-diff-tab",
                )
            if spec is None:
                return None
            if spec.archived:
                return ArchivedSessionTab(
                    spec.session_id,
                    spec.cwd or self.cwd,
                    self._tab_title(spec.cwd or self.cwd),
                    pinned_name=spec.pinned_name,
                    id=_restore_pane_id(spec.session_id),
                    # v0.56.0: read-only is now the FALLBACK, so the tab
                    # says which of the reasons it was.
                    resume_note=spec.resume_note,
                )
            pane = self._restored_pane(spec, leaf)
            if not first_pane:
                first_pane.append(pane)
                pane._boot_report = self._restore_report
            return PaneTab(
                self._tab_title(), pane, id=_restore_pane_id(spec.session_id),
            )

        def _group(node: "layout_mod.Group") -> "Any":
            tabs: "list[Any]" = []
            active_id = ""
            for index, leaf in enumerate(node.tabs):
                if leaf.session_id in placed and not leaf.is_diff:
                    continue
                tab = _tab_for(leaf)
                if tab is None:
                    continue
                placed.add(leaf.session_id)
                if index == node.active or not active_id:
                    # ``initial=`` must name a tab that EXISTS: v0.91.0
                    # measured what happens when it does not -- Textual's
                    # ContentSwitcher hangs waiting for it, surfacing as a
                    # Pilot timeout before a single assertion runs. So the
                    # saved active index only wins if its tab survived, and
                    # the first surviving tab is the standing fallback.
                    if index == node.active or not tabs:
                        active_id = tab.id or ""
                tabs.append(tab)
            return self._make_group(*tabs, active_id=active_id)

        if tree is None:
            pane = self._make_pane(self._engine_factory)
            self._startup_waiting_panes.add(pane)
            pane._boot_report = self._restore_report
            return split_mod.chain(
                self._make_group(self._make_tab(pane, id=self._FALLBACK_PANE_ID))
            )
        root = split_mod.build(tree, _group)
        if not first_pane:
            # Every resolved tab was archived: the window would otherwise
            # have no session in it at all -- no prompt, nothing Ctrl+W
            # could close without closing the app. One fresh tab alongside
            # the archives, carrying the report, is the same answer
            # doxa.cli's own "everything is dead" branch gives. It joins
            # the FIRST group rather than opening a second one: an archive
            # and its replacement are not two regions of work.
            pane = self._make_pane(self._engine_factory)
            self._startup_waiting_panes.add(pane)
            pane._boot_report = self._restore_report
            first = split_mod.first_group(root)
            if first is not None:
                first._tabs.append(
                    self._make_tab(pane, id=self._FALLBACK_PANE_ID)
                )
        return root

    def _initial_active_tab_id(self) -> str:
        """Which tab should be ACTIVE on first mount -- decided here,
        before anything is mounted, and handed to ``TabbedContent``'s own
        ``initial=`` (see :meth:`compose`) rather than set reactively
        later from :meth:`_activate_initial_tab`, which is what this
        replaces and is where the OLD long version of this comment lived.

        **The race this closes, measured rather than assumed.** Textual's
        ``Tabs`` widget defaults itself to its first tab on ITS OWN mount
        (``Tabs._on_mount``) whenever nothing else names one at
        construction, and that default reaches ``TabbedContent.active`` as
        a MESSAGE -- ``Tabs.TabActivated``, handled by
        ``TabbedContent._on_tabs_tab_activated`` -- not a synchronous
        write. The old code set ``tabbed.active`` directly from
        ``App.on_mount``, which runs later than ``Tabs._on_mount`` and so
        USUALLY reads as "after the default, and therefore winning." But
        the queued default-tab message does not evaporate because
        something else wrote ``active`` in between: whenever THAT message
        is finally processed, its handler sets ``active`` to whatever tab
        IT named, unconditionally -- silently overwriting an explicit
        choice made after it was queued but before it was handled. Under
        load (CI: 1 failure; never reproduced locally at any rep count)
        that two-writer race can resolve either way, and the FakeEngine
        specs in ``tests/test_tabsets.py`` resolve fast enough for it to
        matter. The exact failure -- ``'sid-1' == 'sid-2'`` -- is a WRONG
        id, not the null v0.38.0 already fixed (:meth:`_persist_tabset`'s
        own ``_activation_pending`` guard), because this is a different
        mechanism: two competing writers, not one write that never came.

        The fix is not to win that race, it is to not run it: if the
        CORRECT tab is the one ``Tabs`` defaults to in the first place --
        because it was TOLD to, via ``initial=`` -- there is no stray
        message from a wrong default left to land later. One writer, one
        value, converges to the right answer however long it takes to
        propagate.

        Selection rule, unchanged from the method this replaces: the saved
        active tab if the record named one -- live pane or archived tab
        alike, it is where the user was -- otherwise the first SESSION
        spec. Read off :attr:`_restore_tabs` rather than mounted panes,
        because nothing is mounted yet; :data:`_FALLBACK_PANE_ID` is the
        one case that needs a name before it exists -- every restored tab
        archived, so :meth:`compose` adds one fresh pane under that fixed
        id, purely so this method has something to call it.

        **v0.97.0: this is the id of the tab the group HOLDING the saved
        active session will open on**, and every other group opens on its
        own saved active tab. Each ``PaneGroup`` passes its own answer to
        its own ``TabbedContent``, so the race above is closed once per
        group by the same mechanism rather than once per window -- and a
        tab id is unique across the window, so this method's contract is
        unchanged for every caller that only ever had one group."""
        if not self._restore_tabs:
            return ""  # one pane; Tabs' own first-tab default is already right
        # A session in a group's tab list names its OWN tab (v0.97.0 --
        # through v0.95.0 it named its tab's FIRST leaf, because a tab held
        # a tree). The one indirection left is the diff surface, which has
        # a tab of its own and never answers for a session.
        tree = self._restore_group_tree()
        if tree is not None:
            for group in layout_mod.groups(tree):
                for leaf in group.tabs:
                    if leaf.is_diff:
                        continue
                    if (
                        self._restore_active_id
                        and leaf.session_id == self._restore_active_id
                    ):
                        return _restore_pane_id(leaf.session_id)
        for spec in self._restore_tabs:
            if not spec.archived:
                return _restore_pane_id(spec.session_id)
        return self._FALLBACK_PANE_ID

    def _activate_initial_tab(self) -> None:
        """Startup's own explicit FOCUS -- activation itself is decided
        earlier now, by :meth:`_initial_active_tab_id` (handed to
        ``TabbedContent`` as ``initial=`` in :meth:`compose`, before
        anything mounts), so this is left with the half of the old
        combined method that a widget only has AFTER it exists: putting
        the keyboard on it.

        An ordinary launch, and a restore with no saved active tab, used
        to get their active tab and their focus as a side effect of the
        first pane focusing its own prompt on mount -- v0.38.0 removed
        that (see :meth:`_focus_tab`'s own docstring) because "the first
        prompt is focused because a widget we do not own happens to
        announce itself" is exactly the implicitness split-panes needs the
        startup leaf not to have. So this still runs from ``App.on_mount``
        and still says explicitly what v0.38.0 wanted said: focus the tab
        that ended up active.

        The lookup here is independent of whether ``TabbedContent.active``
        has itself finished propagating by this point (that is a SEPARATE
        question from the one ``_initial_active_tab_id`` answers, and this
        method does not need it resolved) -- it re-derives the same target
        by id, the same way :meth:`_initial_active_tab_id` chose it,
        querying the mounted tree instead of :attr:`_restore_tabs` because
        panes exist now."""
        try:
            tabbed = self._strip()
        except Exception:  # noqa: BLE001 -- no tab strip, nothing to choose
            return
        target: "Any" = None
        if self._restore_active_id:
            # The LEAF, not the tab (v0.91.0). A restored split puts three
            # sessions in one tab, and "restore the saved active tab"
            # under-specifies which of them the keyboard belongs to --
            # which is the same defect the saved active TAB had from
            # v0.23.0 to v0.32.0, one level down. The leaf carries a
            # derived id for exactly this lookup.
            leaf_id = f"{_restore_pane_id(self._restore_active_id)}-leaf"
            target = next((p for p in self.panes() if p.id == leaf_id), None)
            if target is None:
                with contextlib.suppress(Exception):
                    target = tabbed.get_pane(
                        _restore_pane_id(self._restore_active_id)
                    )
        if target is None:
            target = next(iter(self.panes()), None)
        if target is None or not target.id:
            return
        self._focus_tab(target)

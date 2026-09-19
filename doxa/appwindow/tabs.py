# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.appwindow.tabs -- a tab's whole life, from opened to gone.

The fourth of DoxaApp's method families to become a mixin, and the one the
other three keep reaching into: what it takes to OPEN a tab, what a tab can
say about the session behind it, and the several different ways one goes
away.

Opening. :meth:`WindowTabsMixin._tab_title` is the label a pane is BORN
with, replaced by the pane's own the moment its engine exists;
:meth:`open_tab_at` is the repo picker's spawn at an explicit path;
:meth:`resume_session` reopens a recorded conversation in a NEW tab, never
in the pane it was asked from, with :meth:`_resume_read_only` for the two
states a resume cannot happen in and :meth:`_attach_in_new_tab` for the one
where the session is still RUNNING and wants attaching rather than
replaying. :meth:`_activate_tab` is the single assignment to
``TabbedContent.active`` that survives both moments Textual's own reactive
refuses it.

What a tab knows about its session. :meth:`_pane_ctx` reads the context
share straight off the engine attribute the ctx chip prints,
:meth:`_pane_repo_root` names the project as an identity key, and
:meth:`_describe_session` renders one :class:`doxa.triage.Facts` fresh every
time -- a label is never stored, because ``display_name()`` is not stable.
Those three were the ``-- the rail's model`` banner in doxa/app.py; it is
stated here instead, because every method it labelled moved out from under
it. They came with the tabs rather than with the rail that displays them:
they are facts about a SESSION, and the rail is only one of the surfaces
that asks.

Renaming, which is editing a label where the label IS:
:meth:`_pane_for_tab`, :meth:`_start_rename` and :meth:`_end_rename`, with
:meth:`_jump_tab_marker` beside them because a label that changes width
moves the active-tab underline and Textual would rather slide it there.

Closing, which is most of this module and most of why the family is one
family. :meth:`_close_group_tab` is the single teardown path for "a surface
is going away" -- the diff toggle's own half, :meth:`_open_diff_beside`,
and :meth:`_close_pane` both reach it. :meth:`_close_pane` is one path with
two dispositions, detach or stop, and closing the LAST tab closes the app
on whichever it was. :meth:`_close_read_only_tab`,
:meth:`_close_archived_tab` and :meth:`_close_transcript_tab` are the kinds
that are not sessions at all; :meth:`_end_session` and :meth:`_stop_active`
are Ctrl+Q and the palette's tab-scoped stop; :meth:`_record_after_close`
captures what the persisted tab set still needs to know about a pane that
has left the strip; and :meth:`_closest_group_heir` and
:meth:`_closest_sibling` decide who inherits the keyboard, measured off the
rectangles the user was actually looking at. :meth:`_reseat_pane` is a
close and an open in one move: nothing in this window re-parents a widget,
so a pane cannot be CARRIED to another group, only rebuilt there around the
same live session.

And getting from one to another: :meth:`_cyclable_tabs`, :meth:`_cycle_tab`
and :meth:`_switch_to_tab`.

The methods move verbatim -- same order, same docstrings, same comments --
after :mod:`doxa.appwindow.failures`, :mod:`doxa.appwindow.restore` and
:mod:`doxa.appwindow.sidebar`. Not one of the twenty-eight carries a
decorator Textual reads: two are ``@staticmethod``, which is a plain
descriptor, and an ``@on`` handler could not have come at all --
``_MessagePumpMeta`` registers handlers by scanning the class body it
builds, and never scans a plain mixin's. That is why four of them stayed
behind in the MIDDLE of this family: ``_on_tab_activated``,
``_on_click_maybe_rename``, ``_on_rename_submitted`` and
``_on_rename_cancelled``. Every ``action_*`` and ``_cmd_*`` method stayed
for a different reason -- they are the actions family, and they move with
it rather than ahead of it.

One moved line is not byte-identical, and the edit is arithmetic:
``_attach_in_new_tab``'s deferred ``from .client import EngineClient`` is
``from ..client`` here, because this module sits one package deeper. No
constant moved, and nothing the suite patches on ``doxa.app`` is read by
anything that did -- neither ``SessionEngine`` nor ``notify_mod`` nor the
module-level ``_stop_session`` appears in the twenty-eight, and
``_stop_session`` stays in doxa/app.py with the comment that says why.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

from textual.app import App
from textual.widgets import TabPane

from .. import history as history_mod
from .. import layout as layout_mod
from .. import peers as peers_mod
from .. import tabsets as tabsets_mod
from .. import transcript as transcript_mod
from .. import triage as triage_mod
from ..session.pane import SessionPane
from ..ui import labels as labels_mod
from ..ui import split as split_mod
from ..ui.dialogs import CloseWithTurnRunning, TabRename
from ..ui.diffview import DiffPane
from ..ui.labels import ellipsize, short_model
from ..ui.prompt import PromptInput
from ..ui.split import PaneGroup, PaneTab
from ..ui.transcript import ArchivedSessionTab, SubagentTranscriptTab


class WindowTabsMixin:
    """DoxaApp's tab-lifecycle half. Mixed into the app, never used
    standalone: every method here reads window state through ``self`` --
    the mounted groups and their strips, the panes and the engines behind
    them, and the records of what was detached or ended this run."""

    def _tab_title(self, cwd: "str | None" = None) -> str:
        """The label a pane is BORN with -- model plus directory, no git.
        Defaults to this app's own ``cwd``; the repo picker's "open in a
        new tab" (item 4) passes the CHOSEN path instead, via
        :meth:`_make_pane_at`, so that tab is never born labelled with the
        wrong directory for one boot.

        The pane replaces it with its own ``auto_label`` the moment its
        engine and GitLine exist (one boot later); this exists so a tab
        never flashes a differently-shaped label on the way there. Two tabs
        on the same repo, branch and model do read alike, deliberately:
        they ARE alike, and the palette's tab section carries the session
        id that tells them apart."""
        self._tab_serial += 1
        name = Path(cwd or self.cwd).name or "session"
        return ellipsize(f"{short_model(self.model)} · {name}")

    def _activate_tab(self, tab: "Any", *, retry: bool = True) -> None:
        """Make TAB the active tab, and survive the two moments Textual's
        own reactive refuses the assignment.

        ``TabbedContent.active`` validates through ``Tabs.validate_active``,
        which raises ``ValueError: No Tab with id …`` whenever the strip
        does not -- yet, or any longer -- hold a header for that pane. Two
        real states reach it, both measured on this branch rather than
        imagined:

        * the tab was added from a WORKER (``/attach``'s own
          ``_cmd_attach_worker``) and the header's mount into ``#tabs-list``
          has not landed by the time the next line runs;
        * the app is being torn down under a worker still finishing an
          attach -- the v0.85.0 defect class, which ``tests/conftest.py``'s
          ``_errors_must_be_claimed`` fixture correctly turns into a test
          failure rather than a silent error block.

        So the assignment is retried ONCE on the next refresh, then given
        up on. Retried rather than suppressed outright, because a new tab
        that silently fails to activate is the "it arrived by accident"
        failure v0.38.0 removed; given up on rather than looped, because
        the teardown case has no later moment in which it could succeed."""
        tab_id = getattr(tab, "id", "") or ""
        if not tab_id:
            return
        try:
            self._strip_for(tab_id).active = tab_id
        except Exception:  # noqa: BLE001 -- see the docstring
            if retry and getattr(tab, "is_mounted", False):
                self.call_after_refresh(self._activate_tab, tab, retry=False)

    async def open_tab_at(self, path: str) -> "str | None":
        """The repo picker's own spawn call (item 4): a fresh session tab
        rooted at an EXPLICIT path, via ``_new_session_factory_at`` -- the
        SAME spawn primitive Ctrl+T (:meth:`action_new_tab`) uses, just
        parametrized by path instead of this app's own launch cwd. Returns
        an error string on a bad path (never raises, never half-creates a
        tab); None on success.

        Activates AND focuses the new tab, in that order and both
        explicitly -- picking a repo out of the picker is as much "take me
        there" as Ctrl+T is, and since v0.38.0 neither activation nor
        focus arrives on its own (see :meth:`_focus_tab`)."""
        if not os.path.isdir(path):
            return f"not a directory: {path}"
        tabbed = self._strip()
        pane = self._make_pane_at(path, lambda: self._new_session_factory_at(path))
        tab = self._make_tab(pane)
        await tabbed.add_pane(tab)
        self._activate_tab(tab)
        self._focus_tab(tab)
        self._persist_tabset()
        return None

    async def resume_session(self, group: dict) -> "str | None":
        """Reopen a past conversation (v0.56.0). Returns a note to show the
        user, or None when there is nothing left to say.

        NEW TAB, not this pane. A resumed conversation is a DIFFERENT
        conversation from the one the active pane is holding -- its own
        history, its own cost, its own transcript file -- and taking the
        pane over would either end that session or orphan it, on a
        keystroke whose stated subject was some other session entirely.
        DOXA already has a verb for "replace what is in this tab"
        (``/clear``, which says so and finalizes first) and a verb for "go
        somewhere else" (the repo picker's open-in-a-new-tab, which this
        mirrors down to the mount/activate/focus order). Resume is the
        second kind. It is also the reversible kind: Ctrl+W closes the tab
        and nothing was lost, whereas an in-pane takeover has no undo.

        A RUNNING session is ATTACHED, never resumed. Resuming means
        handing ``--resume <id>`` to a second CLI process while the first
        is still alive on that conversation, which is two writers on one
        transcript and two daemons under one registry id -- so this
        detects it (the peer registry, the same reaped view ``doxa
        attach`` reads) and does the thing the user actually wanted
        instead: attaches to the live daemon, in a new tab, and says so.
        Not a silent substitution and not a fork; a different, correct
        act, named.

        Every refusal comes back as a STRING the caller prints. Nothing
        here raises, and nothing here half-creates a tab."""
        session_id = str(group.get("session_id") or "")
        cwd = str(group.get("cwd") or "")
        title = str(group.get("title") or "").strip()
        state, reason = await asyncio.to_thread(
            history_mod.resume_state, session_id, cwd
        )
        if state == history_mod.RESUME_RUNNING:
            return await self._attach_in_new_tab(session_id, title)
        if state != history_mod.RESUME_OK:
            return await self._resume_read_only(session_id, cwd, title, reason)
        # Already open in this window? Then the answer is the tab that has
        # it, not a second one beside it -- and since it is open, it is
        # also running, which the registry check above would normally have
        # caught; this covers the in-process (no registry entry) case.
        for pane in self.panes():
            if pane._session_id == session_id:
                self._focus_tab(pane)
                self._strip_for(pane.tab_id or "").active = (
                    pane.tab_id or ""
                )
                return f"{session_id[:8]} is already open in this window."
        tabbed = self._strip()
        pane = self._make_pane_at(
            cwd, lambda: self._resume_session_factory(cwd, session_id)
        )
        # Born labelled with the conversation's own title where it has
        # one: a resumed tab whose label says "opus · doxa" like every
        # other tab makes the user find it by elimination. The pane's
        # auto_label takes over one boot later, exactly as for any tab.
        if title:
            pane.custom_name = title[:40]
        # Read once by _boot, which reuses v0.32.0's transcript restore to
        # draw the prior turns -- see SessionPane._restore_transcript.
        pane._resume_from = session_id
        tab = self._make_tab(pane)
        await tabbed.add_pane(tab)
        self._activate_tab(tab)
        self._focus_tab(tab)
        self._persist_tabset()
        return None

    async def _resume_read_only(
        self, session_id: str, cwd: str, title: str, reason: str,
    ) -> "str | None":
        """:meth:`resume_session`'s own fallback (v0.93.0), for the two
        states :func:`history.resume_state` answers when resuming truly
        cannot happen: ``RESUME_NO_CWD`` (the directory is gone) and
        ``RESUME_NO_HISTORY`` (a pre-v0.56.0 conversation the CLI's own
        store never learned this id under). Through v0.91.0 both landed
        here as a bare refusal string -- "cannot resume ... — <reason>" --
        an error where a real answer was sitting on disk the whole time:
        DOXA's OWN transcript (:mod:`doxa.transcript`, the same
        ``$LORE_PROJECTS_DIR/<slug>/<id>.jsonl`` /search already indexes)
        is a SEPARATE store from the CLI's own resume history that
        :func:`history.resume_state` just found lacking, and neither
        failure reason says anything about whether IT exists.

        So this reaches for the exact read-only surface a dead-daemon
        BOOT restore already falls back to -- :class:`ArchivedSessionTab`,
        the same ``mount_transcript`` call, the same ``resume_note``
        banner explaining WHY it is read-only (see that class's own
        v0.56.0 note: "read-only" with no reason reads as the feature
        having silently not happened) -- rather than building a second
        transcript viewer for the same fact. A session with no transcript
        on disk EITHER falls through to the plain refusal unchanged: there
        is truly nothing to show, and an empty archived tab would be a
        worse answer than the honest words that were already here.

        An already-open archived tab for this SAME session (from an
        earlier read-only resume, or from this window's own boot restore)
        is reused rather than duplicated -- the same "already open" rule
        the top of :meth:`resume_session` applies to a live pane."""
        if not await asyncio.to_thread(transcript_mod.exists, session_id, cwd):
            return f"cannot resume {session_id[:8]} — {reason}"
        existing = next(
            (t for t in self.archived_tabs() if t.session_id == session_id), None,
        )
        if existing is not None:
            self._activate_tab(existing)
            self._focus_tab(existing)
            return f"{session_id[:8]} is already open here, read-only."
        tabbed = self._strip()
        tab = ArchivedSessionTab(
            session_id, cwd, self._tab_title(cwd or self.cwd),
            pinned_name=(title[:40] if title else None),
            id=f"resume-ro-{session_id}",
            resume_note=reason,
        )
        await tabbed.add_pane(tab)
        self._activate_tab(tab)
        self._focus_tab(tab)
        self._persist_tabset()
        return None

    async def _attach_in_new_tab(
        self, session_id: str, title: str
    ) -> "str | None":
        """A resume aimed at a session that is still RUNNING: attach to its
        daemon instead, in a new tab.

        A new tab rather than the palette's own in-pane attach
        (``_cmd_attach``, which switches the ACTIVE pane's engine): the
        user arrived here from a search result, not from "put something
        else in this tab", and the promise the confirm dialog makes is a
        new tab either way. Same non-destructive property -- whatever the
        current pane holds is still there afterwards.

        An in-process session with no daemon socket cannot be attached to
        at all, and is refused in words rather than quietly resumed: a
        second CLI on a live conversation is exactly what this branch
        exists to avoid."""
        from ..client import EngineClient  # deferred: no daemon, no import

        entry = next(
            (e for e in peers_mod.read_registry() if e.session_id == session_id),
            None,
        )
        socket_path = getattr(entry, "daemon_socket", "") if entry else ""
        if not socket_path:
            return (
                f"{session_id[:8]} is still running, but not behind a daemon "
                "this window can attach to (an in-process session). it is "
                "not resumable while it runs — end it first, or use the "
                "window that owns it."
            )
        tabbed = self._strip()
        pane = self._make_pane_at(
            str(getattr(entry, "cwd", "") or self.cwd),
            lambda: EngineClient(socket_path),
        )
        if title:
            pane.custom_name = title[:40]
        # An ATTACH, so the v0.32.0 restore path applies with its own
        # precondition intact: the daemon has a ring it may replay, and
        # the transcript is only drawn once it has agreed to skip it.
        pane._restore_transcript_wanted = True
        tab = self._make_tab(pane)
        await tabbed.add_pane(tab)
        self._activate_tab(tab)
        self._focus_tab(tab)
        self._persist_tabset()
        return (
            f"{session_id[:8]} is still running — attached to it in a new "
            "tab rather than resuming it. a live conversation has one "
            "writer, and a second would fork it."
        )

    @staticmethod
    def _pane_ctx(pane: "Any") -> "float | None":
        """This session's context share, or ``None`` when its limit was
        never reported.

        The CLI's OWN accounting, read straight off the engine attribute
        the ctx chip prints (:attr:`doxa.engine.Engine.last_ctx_percentage`)
        -- not a second measurement, and not a guess. ``None`` stays
        ``None`` all the way to the glyph, where it renders nothing at
        all: ``/context``'s rule that an unreported limit reads ``?`` and
        stays ``?``, one level down. Treating it as 0% would turn an
        honesty rule into a wrong answer -- the rail would say "plenty of
        room" about a window it never measured."""
        engine = getattr(pane, "engine", None)
        value = getattr(engine, "last_ctx_percentage", None)
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _pane_repo_root(pane: "Any") -> str:
        """The project this session belongs to, as an identity key.

        ``GitLine.main_root`` (v1.2.0), which is the MAIN checkout's root
        even from inside a linked worktree and costs no subprocess: the
        pane's status line already resolved it at construction. Falling
        back to the pane's cwd rather than to "" keeps two sessions in
        one non-repo directory grouped together, which is the same answer
        :attr:`doxa.peers.PeerInfo.scope_key` gives (``repo_root or
        cwd``)."""
        git = getattr(pane, "_git", None)
        root = getattr(git, "main_root", None) if git is not None else None
        if root:
            return str(root)
        engine = getattr(pane, "engine", None)
        return str(getattr(engine, "cwd", None) or getattr(pane, "cwd", "") or "")

    def _describe_session(
        self, session_id: str, surfaces: "dict[str, Any] | None" = None
    ) -> "triage_mod.Facts":
        """One session's :class:`doxa.triage.Facts` for the rail.

        The label is rendered FRESH every time and never stored: a
        collection records session IDS because ``display_name()`` is not
        stable -- it changes when a session is renamed and again when its
        first prompt lands.

        The marks come from :func:`doxa.ui.labels.mark_over`, the same
        derivation a group's ``Tab`` header uses. Not a second reading of
        what a mark MEANS -- one source, read twice, which is the risk the
        spec names and this is the answer to it.

        An UNMOUNTED session still gets a label: its pinned name from the
        record this run kept, or its short id. It never gets marks (there
        is no pane to have earned one) and it is reported ``mounted=False``
        so the row can say so rather than pretend.

        ``surfaces`` is :meth:`_sidebar_surfaces`' one-pass map when the
        caller built one; ``None`` falls back to the single-id lookup, so
        this stays callable on its own."""
        surface = (
            self._sidebar_pane(session_id)
            if surfaces is None else surfaces.get(session_id)
        )
        if isinstance(surface, SessionPane):
            marks = tuple(
                name for name in labels_mod.TAB_STATE_MARKS
                if labels_mod.mark_over([surface], name)
            )
            return triage_mod.Facts(
                label=surface.display_name(),
                marks=marks,
                mounted=True,
                ctx_percentage=self._pane_ctx(surface),
                state=triage_mod.STATE_LIVE,
                repo_root=self._pane_repo_root(surface),
            )
        if surface is not None:  # an ArchivedSessionTab: read-only, no marks
            return triage_mod.Facts(
                label=getattr(surface, "base_label", "") or session_id[:8],
                mounted=True,
                # An archived tab is a read-only record of a session that
                # is already over: ENDED, and therefore old, whether or
                # not THIS run is the one that ended it.
                state=triage_mod.STATE_ENDED,
                repo_root=str(getattr(surface, "cwd", "") or ""),
            )
        detached = self._detached_this_run.get(session_id)
        record = detached or self._ended_this_run.get(session_id)
        pinned = getattr(record, "pinned_name", None) if record else None
        return triage_mod.Facts(
            label=(pinned or session_id[:8]),
            mounted=False,
            # DETACHED is not OLD. A detached session is live and may be
            # doing work right now -- see doxa.triage.OLD_STATES for the
            # whole of that decision and what it deliberately excludes.
            state=(
                triage_mod.STATE_DETACHED if detached is not None
                else triage_mod.STATE_ENDED
            ),
            repo_root=str(getattr(record, "cwd", "") or "") if record else "",
        )

    def _pane_for_tab(self, tab: Any) -> "SessionPane | None":
        from textual.widgets._tabbed_content import ContentTab

        pane_id = ContentTab.sans_prefix(tab.id or "")
        for pane in self.panes():
            if pane.tab_id == pane_id:
                return pane
        return None

    async def _start_rename(self, pane: "SessionPane") -> None:
        """Mount the editor in the tab's own slot and hide the tab behind
        it, so the label is edited where the label IS."""
        if self.query("#tab-rename"):
            return  # one rename at a time
        with contextlib.suppress(Exception):
            tabbed = self._strip_for(pane.tab_id)
            tab = tabbed.get_tab(pane.tab_id)
            editor = TabRename(pane.tab_id, pane.display_name())
            editor.styles.width = max(len(editor.value) + 4, 14)
            await tab.parent.mount(editor, before=tab)
            tab.display = False
            editor.focus()

    def _end_rename(self, pane_id: str) -> None:
        with contextlib.suppress(Exception):
            tabbed = self._strip_for(pane_id)
            tabbed.get_tab(pane_id).display = True
        for editor in list(self.query(TabRename)):
            editor.remove()
        pane = next((p for p in self.panes() if p.tab_id == pane_id), None)
        if pane is not None:
            with contextlib.suppress(Exception):
                pane.query_one("#prompt-input", PromptInput).focus()

    def _jump_tab_marker(self) -> None:
        """Put the active-tab underline at its destination on THIS frame.

        Textual's ``Tabs`` slides the marker: ``watch_active`` calls
        ``_highlight_active(animate=True)``, which arms a 0.02 s timer and
        then animates ``highlight_start``/``highlight_end`` over 0.3 s.
        ``animation_level = "none"`` (set in __init__) already takes the
        no-animate branch, but that branch still defers the move to
        ``call_after_refresh`` -- one frame late. Measured: the slide cost
        ~290-345 ms of WALL time per switch on top of the switch itself,
        constant regardless of scrollback, which is exactly the "tab
        switching is laggy" report.

        So the marker is placed directly, from the same geometry Textual's
        own mover reads. Failure is not an error: if Textual's internals
        move, this degrades to the built-in (still un-animated) path rather
        than breaking tab switching."""
        with contextlib.suppress(Exception):
            from textual.widgets import Tabs
            from textual.widgets._tabs import Underline

            tabs = self._strip().query_one(Tabs)
            active = tabs.query_one("#tabs-list > Tab.-active")
            start, end = active.virtual_region.shrink(
                active.styles.gutter
            ).column_span
            if end <= start:
                return  # geometry not laid out yet: leave the marker alone
            underline = tabs.query_one(Underline)
            underline.highlight_start = start
            underline.highlight_end = end

    async def _open_diff_beside(self, pane: "SessionPane") -> "str | None":
        """The OPEN half of :meth:`toggle_diff_pane`, extracted in v1.0.1
        so the auto-open setting reaches it without going through a
        toggle -- an automatic open must never be able to CLOSE a diff
        the user is reading, which is what calling the toggle blind would
        do the moment one was already there.

        Every rule the hand-driven open follows is here and nowhere else:
        the free box, the :func:`doxa.layout.split_refusal` floor, the
        ``ROW`` orientation, and the deliberate absence of a focus
        call."""
        if not pane._session_id:
            return "this session has not started yet — nothing to diff"
        # The group holding THIS pane, falling back to the focused one:
        # with a caller-supplied pane (the chip, the auto-open) the
        # keyboard may be somewhere else entirely, and the diff has to
        # land beside the session it is a diff of.
        group = split_mod.group_of(pane) or self.focused_group()
        if group is None:
            return "there is no pane group here to diff"
        box = split_mod.free_box(group)
        if box is None:
            return (
                f"this pane is already split as deep as DOXA goes "
                f"({layout_mod.SPLIT_SLOTS} levels) — close a pane and "
                "try again"
            )
        region = group.region
        refusal = layout_mod.split_refusal(
            region.width, region.height, layout_mod.ROW
        )
        if refusal is not None:
            return refusal
        diff = DiffPane(
            pane._session_id,
            str(getattr(pane.engine, "cwd", None) or pane.cwd),
            id=f"{pane.id}-diff",
        )
        # The spec's own design check, answered by construction: the diff
        # goes into a GROUP's tab, and nothing about the group had to be
        # special-cased for it -- a group's tab list is a list of surfaces
        # and a diff is a surface. Beside the session rather than in the
        # same group's strip, because the point of a live diff is looking
        # at it WHILE you type; the same tab list would hide one behind the
        # other. Both statements are true at once, and that is what makes
        # the model right rather than merely accommodating.
        diff_tab = PaneTab(self._tab_title(), diff, id=f"{pane.id}-diff-tab")
        await box.mount(split_mod.chain(self._make_group(diff_tab)))
        box.divide(layout_mod.ROW)
        # Focus STAYS in the session. A split spawns a session you asked
        # to work in, so v0.91.0 moves the keyboard there; a diff is
        # something you asked to LOOK at while you keep typing, and
        # moving the keyboard out of the prompt to open it would be the
        # opposite of the feature. "Visible and focused are different
        # states" cuts both ways, and this is the other way.
        #
        # v1.0.1 makes that load-bearing rather than merely tidy: the
        # `auto_diff` setting opens this pane while the user is typing,
        # unasked. A surface that took the keyboard on its way in would
        # eat the next characters of a prompt someone is mid-sentence
        # with -- so the absence of a `_focus_tab` call here is asserted
        # by tests/test_diff_chip.py, not just described.
        self._persist_tabset()
        return None

    async def _close_group_tab(self, surface: "Any") -> None:
        """Take ONE tab out of its group, and take the group with it when
        that was its last.

        The single teardown path for "a surface is going away" -- the diff
        toggle and :meth:`_close_pane` both reach it. Two levels of
        collapse, in order, because they are two different facts: a group
        that still has tabs keeps its region and shows another tab; a group
        with none is not a region any more, and the split above it collapses
        by exactly the rule v0.91.0 wrote for a leaf.

        Awaits each removal rather than firing and forgetting: the NEXT
        step reads the parent's child list, and Textual's ``Widget.remove``
        only takes effect when its ``AwaitRemove`` is awaited."""
        group = split_mod.group_of(surface)
        tab = surface.parent
        while tab is not None and not isinstance(tab, TabPane):
            tab = tab.parent
        if group is None or tab is None:
            with contextlib.suppress(Exception):
                await surface.remove()
            return
        remaining = [t for t in group.tabs() if t is not tab]
        with contextlib.suppress(Exception):
            await self._strip_for(tab.id or "").remove_pane(tab.id or "")
        if remaining:
            return
        box = split_mod.owning_box(group)
        with contextlib.suppress(Exception):
            await group.remove()
        await split_mod.prune_boxes(box)

    async def _reseat_pane(self, pane: "SessionPane", target: "PaneGroup") -> "str | None":
        """Re-create ``pane`` as a tab of ``target`` and tear down the
        original, carrying the live session across.

        The order is the load-bearing part, and every step of it exists
        because of the no-re-parenting constraint:

        1. take the engine handle OFF the source pane, so its teardown
           cannot stop or detach a session that is about to keep running;
        2. mount the new pane in the destination, and only then hand it the
           handle -- a pane that boots before it is adopted would spawn a
           SECOND session, which is the failure this ordering prevents;
        3. remove the source tab, collapsing nothing (the source keeps its
           other tabs, which :meth:`move_tab_to_group` guaranteed).

        Never raises: a half-completed move would leave a session with no
        view onto it, which is worse than a refusal."""
        engine = pane.engine
        session_id = pane._session_id
        if engine is None or not session_id:
            return "this session has not started yet — nothing to move"
        name = pane.custom_name
        cwd = str(getattr(engine, "cwd", None) or pane.cwd)
        ratio = layout_mod.clamp_prompt_ratio(getattr(pane, "prompt_ratio", 0.0))
        marks = dict(getattr(pane, "_marks", {}))
        source_tab = pane.tab
        # 1. Release the session from the pane that is going away. From
        #    here the daemon has no view onto it, which is a state DOXA is
        #    already fluent in -- it is exactly what Ctrl+W leaves behind.
        pane.release_engine()
        # 2. The new view. Its "factory" hands back the handle that is
        #    already running rather than building one, and ``_adopted``
        #    is what stops ``_boot`` calling ``start()`` on it -- the one
        #    line between "the session moved" and "a second CLI is now
        #    writing this transcript".
        fresh = self._make_pane_at(cwd, lambda: engine)
        fresh._adopted = True
        fresh._session_id = session_id
        if name:
            fresh._initial_pinned_name = name
        fresh.prompt_ratio = ratio
        # The scrollback comes back from DISK, the same v0.32.0 path a
        # reattach uses: the widget is new, so the blocks the old one had
        # painted went with it, and re-reading the transcript is the only
        # honest way to put them back.
        fresh._restore_transcript_wanted = True
        new_tab = self._make_tab(fresh)
        try:
            await target.tabbed.add_pane(new_tab)
        except Exception:  # noqa: BLE001 -- destination went away mid-move
            pane.adopt_engine(engine, session_id)
            return "that pane group is no longer there"
        for class_name, value in marks.items():
            if value:
                fresh._marks[class_name] = True
        # 3. The source tab goes, and the session it was showing does not.
        with contextlib.suppress(Exception):
            await self._strip_for(source_tab.id or "").remove_pane(
                source_tab.id or ""
            )
        self._activate_tab(new_tab)
        self._focus_tab(fresh)
        self._persist_tabset()
        return None

    async def _close_read_only_tab(self) -> bool:
        """Close the active tab when it is one of the two READ-ONLY kinds,
        and say whether it closed one.

        The two -- a subagent transcript (SubagentTranscriptTab) and a
        restored archive (ArchivedSessionTab) -- share the property that
        makes this one method: neither is a session. ``self.active_pane``
        is SessionPane-only and comes back None for both, so there is no
        daemon to detach, no engine to stop and no turn-in-flight question
        to ask. Each still needs ITS own teardown (a transcript drops the
        owning pane's reference so reopening builds a fresh one; an
        archive re-persists the tab set so closing it is what takes it
        out) -- hence the dispatch rather than one remove_pane call.

        There is always at least one SessionPane beside them, so neither
        is ever "the last tab" and neither reaches the close-the-app
        branch :meth:`_close_pane` falls back to.

        Extracted in v0.58.0 so BOTH close keys reach it. Ctrl+W has
        called this path since these tabs existed; Ctrl+Q did not, and
        stopped dead on them (see :meth:`_end_session`). The boolean is
        the load-bearing part of the signature: it lets a caller tell
        "closed a read-only tab" from "there was nothing here I know how
        to close", which is what a NEW tab kind will hit -- v0.46.0 shipped
        the (now-removed) beliefs browser unclosable for exactly one
        release by not having a shared answer here."""
        active: "Any" = None
        with contextlib.suppress(Exception):
            active = self._strip().active_pane
        if isinstance(active, SubagentTranscriptTab):
            await self._close_transcript_tab(active)
            return True
        if isinstance(active, ArchivedSessionTab):
            await self._close_archived_tab(active)
            return True
        return False

    async def _close_archived_tab(self, tab: "ArchivedSessionTab") -> None:
        """Ctrl+W on an archived tab (v0.32.0): nothing to detach, nothing
        to stop -- the session ended before this window opened. It closes,
        and closing it is the ONE way to take it out of the persisted set:
        an archived tab the user leaves open comes back at the next launch
        exactly like a live one, which is the whole point.

        Never the last tab: compose() guarantees a SessionPane beside any
        archive, so this never reaches the close-the-app branch."""
        with contextlib.suppress(Exception):
            await self._strip_for(tab.id or "").remove_pane(
                tab.id or ""
            )
        self._persist_tabset()

    async def _end_session(self) -> None:
        pane = self.active_pane
        if pane is None:
            await self._close_read_only_tab()
            return
        if pane.turn_in_flight:
            choice = await self.push_screen_wait(CloseWithTurnRunning())
            if choice == "cancel":
                return
            if choice == "detach":
                await self._close_pane(pane, terminate=False)
                return
        await self._close_pane(pane, terminate=True)

    async def _close_transcript_tab(self, tab: "SubagentTranscriptTab") -> None:
        """Ctrl+W (or the palette's Close tab) on a subagent transcript
        tab: no engine to stop, no daemon to detach -- just remove it and
        drop the owning pane's own reference to it."""
        tab.owner._transcript_tabs.pop(tab.call_id, None)
        with contextlib.suppress(Exception):
            await self._strip_for(tab.id or "").remove_pane(
                tab.id or ""
            )

    def _record_after_close(
        self, pane: "SessionPane", target: "dict[str, tabsets_mod.TabRecord]"
    ) -> None:
        """Scope-checked capture into ``_detached_this_run`` or
        ``_ended_this_run``, called BEFORE the caller's ``remove_pane``
        takes ``pane._session_id`` out of :meth:`panes`'s own scan with it
        -- the two dicts a pane leaving the strip this run can still need
        to be found in, and the same question either way: was this tab
        ever part of THIS window's own repo-scoped persisted set to begin
        with?

        Scope-checked (item 4's repo picker reconciliation, same reasoning
        as _persist_tabset's own exclusion): a cross-repo tab (opened via
        the repo picker) was never part of THIS window's own repo-scoped
        persisted set, so detaching or ending it must not add it there
        either -- its daemon's PeerHost already wrote its own registry
        entry under its own scope key."""
        if not pane._session_id:
            return
        pane_cwd = str(getattr(pane.engine, "cwd", None) or pane.cwd)
        pane_scope = peers_mod.main_repo_root_of(pane_cwd) or pane_cwd
        app_scope = peers_mod.main_repo_root_of(self.cwd) or self.cwd
        if pane_scope != app_scope:
            return
        target[pane._session_id] = tabsets_mod.TabRecord(
            pane._session_id, pane.custom_name,
        )

    async def _close_pane(self, pane: "SessionPane", terminate: bool) -> None:
        """One close path, two dispositions. Closing the LAST tab closes the
        app on the same disposition -- a window with no tabs is not a
        window, and the session's fate must not depend on tab arithmetic.

        A closing session takes its OWN open transcript tabs down with it
        first -- they have no engine and nothing left to route events into
        once the session that spawned their subagents is gone.

        ``is_last`` (v0.85.0) is computed up front and, since v0.99.1, no
        longer decides much: Ctrl+Q now excludes the closing session from
        the persisted restore set unconditionally (below), so the ONLY
        thing tab arithmetic still changes is Ctrl+W's disposition -- see
        the branches below for why -- and :meth:`_cyclable_tabs`'s sibling
        fix for the OTHER half of the v0.85.0 report.

        **Ctrl+Q ends it, Ctrl+W parks it** is the rule as of v0.99.1: a
        terminated session leaves the persisted set no matter which tab it
        was, a detached one stays in it unless it was the last tab (see
        the ``is_last`` branch below for that one's own reasoning, shared
        with Ctrl+Q's last-tab case). Reported live: *"tabs that i had
        closed using CTRL+Q are resurrected on the next start of DOXA
        anyway"* -- and, once told a finalized session cannot be resumed,
        *"but all of those sessions are resumed and dont disappear ...
        there is no way to permanently close a tab."* Both true: v0.60.0
        kept an ended session's id in the persisted set on purpose (the
        `if not is_last` guard this replaces), reasoning that a finalized
        session was still a resumable one, so the record of the tab was
        worth keeping even once the daemon behind it was gone. What it
        missed is that finalize() never removes the conversation from the
        CLI's OWN history store -- so doxa.cli's restore triage
        (``ended_tab_spec`` -> ``history_mod.resume_state``) found it,
        answered RESUME_OK, and handed it back as a live, resumable tab,
        not the read-only one the fix's own comments describe. Nothing
        about the TRANSCRIPT changes here -- it is still on disk, still
        findable by /search and the resume picker -- only whether Ctrl+Q'd
        session ever lands in the file :meth:`_persist_tabset` reads on
        the NEXT launch."""
        for tab in list(pane._transcript_tabs.values()):
            await self._close_transcript_tab(tab)
        is_last = len(self.panes()) == 1
        if terminate:
            note = await pane.stop()
            if note:
                # The pane itself is about to be removed (or the whole app
                # quits, below) -- a toast is screen-level, not pane-level,
                # so it survives the tab it was about -- unlike a SystemBlock
                # mounted in the closing pane's own block list, which the
                # user would never get a chance to see.
                self.notify(note, severity="information", timeout=10)
            # Recorded regardless of is_last (v0.99.1): _ended_this_run no
            # longer feeds the persisted set (see its own docstring), so
            # there is no longer a reason to skip this on the last tab --
            # it is pure in-run bookkeeping now (the sidebar rail's dimmed
            # row for an ended session, for the rest of THIS run only).
            # pane.stop() above already marked the pane _stopped, which is
            # what actually keeps it out of the next launch's restore set
            # -- see _persist_tabset's own mounted-pane scan.
            self._record_after_close(pane, self._ended_this_run)
        else:
            # Detached ON PURPOSE: it is no longer this window's to end, so
            # a later quit-stop leaves it running.
            label = pane.display_name()
            pane.detached_on_purpose = True
            await pane.detach()
            # Reported live: "when the tab is detached with CTRL+W, there
            # should be a notification or message" -- Ctrl+W used to
            # detach in total silence, the tab just gone with no sign the
            # session was still alive anywhere. Same screen-level toast
            # mechanism as the "kept <worktree>" note above, naming the
            # tab and how to get it back -- true whether or not this is
            # the last tab (the daemon keeps lingering either way).
            self.notify(
                f"{label} detached — still running in the background; "
                "bring it back with /attach or the peers chip",
                severity="information", timeout=10,
            )
            if not is_last:
                # Item D #4: this session STAYS in the persisted tab set
                # even though its tab is about to leave the strip below --
                # record it here, before remove_pane takes
                # pane._session_id out of panes()'s own scan with it.
                # Skipped when this IS the last tab -- see below.
                self._record_after_close(pane, self._detached_this_run)
        if is_last:
            # The window's whole tab strip is about to go empty, and the
            # app quits right below -- the reported defect: "if the last
            # remaining open tab is closed with CTRL+Q, the next time doxa
            # is started should start with a fresh session. If last open
            # tab was closed with CTRL+W, we also start with a fresh
            # session, but the old session could be reattached." Both keys
            # close the LAST tab the same way here: the closing session is
            # excluded from _persist_tabset's own mounted-pane scan via
            # `exclude_session_id` -- NOT by removing the pane from the
            # strip first. An earlier version of this fix called
            # remove_pane() here before persisting, to get the same
            # exclusion out of the mounted-pane scan -- which worked, but
            # unmounted a pane with a still-running _peer_pump worker
            # moments before action_quit tore the app down under it: an
            # intermittent AssertionError out of that worker's own `assert
            # self.engine is not None`, surfaced as a visible in-app error
            # block on the way out. The pane now stays mounted, exactly as
            # App.action_quit already handled it before this whole feature
            # existed -- only what _persist_tabset WRITES changes.
            #
            # For Ctrl+Q this `exclude_session_id` is belt-and-suspenders
            # since v0.99.1: pane.stop() above already marked the pane
            # _stopped, which the mounted-pane scan now excludes on its
            # own (same rule as the non-last branch below). Still load-
            # bearing for Ctrl+W: a detached pane is never _stopped, so
            # nothing else here would keep it out of THIS one snapshot
            # before the pane is unmounted. The next launch reads an empty
            # (or unaffected-by-this-tab) record and starts fresh either
            # way; what differs is not the record, it is whether the
            # session is still THERE to /attach back to: Ctrl+Q's is gone
            # (pane.stop() above), Ctrl+W's keeps running (pane.detach()
            # above, and the toast just said so) -- reachable by NAME from
            # here on, never by an automatic restore. See doxa.tabsets'
            # module docstring for the restore side of this distinction.
            self._persist_tabset(exclude_session_id=pane._session_id)
            await App.action_quit(self)
            return
        # Ctrl+Q's pane is still mounted here (removed only below, by
        # _close_group_tab) but already _stopped -- this snapshot already
        # excludes it via _persist_tabset's own mounted-pane scan, no
        # `exclude_session_id` needed. A Ctrl+W'd pane is not _stopped, so
        # it is written here exactly as it was before -- still in the set,
        # per item D #4.
        self._persist_tabset()
        # **Closing a tab closes ONE session** (v0.97.0, and the third of
        # the three problems the inversion dissolves rather than patches).
        # Through v0.95.0 this pane's tab could hold two more sessions and
        # closing it ended all three; a tab holds one surface now, so the
        # question is only what INHERITS the keyboard.
        #
        # Two collapses, in order, and they are different facts:
        #   * the group has other tabs   -> it keeps its region, shows one
        #   * the group has none left    -> the region goes, the split
        #                                   above it collapses, and the
        #                                   nearest surviving group takes
        #                                   the keyboard.
        # The keyboard's destination is named explicitly for the reason
        # every other focus move in this file is (v0.38.0): a pane
        # disappearing is not a user saying where to go next.
        group = split_mod.group_of(pane)
        siblings = [t for t in group.tabs() if t is not pane.tab] if group else []
        heir: "Any" = None
        if not siblings:
            heir = self._closest_group_heir(group)
        await self._close_group_tab(pane)
        if heir is not None:
            self._focus_tab(heir)
        else:
            self._focus_active_tab()
        self._persist_tabset()

    def _closest_group_heir(self, closing: "PaneGroup | None") -> "Any":
        """Which surface inherits the keyboard when a whole GROUP closes:
        the active tab of the group nearest it on screen, measured from the
        rectangles the user was actually looking at.

        The group-level twin of :meth:`_closest_sibling`, and the same
        rule: nearest by painted position, falling back to the first
        remaining group when nothing has been painted yet."""
        if closing is None:
            return None
        here = closing.region
        best = None
        best_gap = None
        for other in self.groups():
            if other is closing:
                continue
            region = other.region
            if region.width <= 0 or region.height <= 0:
                continue
            gap = abs(region.x - here.x) + abs(region.y - here.y)
            if best_gap is None or gap < best_gap:
                best, best_gap = other, gap
        if best is None:
            best = next((g for g in self.groups() if g is not closing), None)
        if best is None:
            return None
        return next(iter(best.surfaces()), None)

    def _closest_sibling(
        self, pane: "SessionPane", siblings: "list[SessionPane]"
    ) -> "SessionPane":
        """Which pane inherits the keyboard when ``pane`` closes: the one
        nearest it on screen, measured from the rectangles the user was
        actually looking at, falling back to the first remaining leaf when
        nothing has been painted yet."""
        here = pane.region
        best = None
        best_gap = None
        for other in siblings:
            region = other.region
            if region.width <= 0 or region.height <= 0:
                continue
            gap = abs(region.x - here.x) + abs(region.y - here.y)
            if best_gap is None or gap < best_gap:
                best, best_gap = other, gap
        return best or siblings[0]

    def _cyclable_tabs(self) -> "list[Any]":
        """Every tab in the FOCUSED GROUP's strip, VISUAL (strip) order,
        for :meth:`_cycle_tab` -- deliberately NOT :meth:`panes` (session
        tabs only; every engine-touching caller needs that narrower list)
        and NOT :meth:`_restorable_tabs` (session + archived, but never a
        subagent transcript, because the persisted set has no use for
        one). Reported live: "CTRL+ArrowLeft ... only seems to work to
        switch among active sessions ... not between read-only finished
        sessions" -- Ctrl+Left/Right must reach every tab a user can SEE,
        an archived read-only tab and an open subagent transcript
        included, because both sit right there in the strip.

        **Scoped to one group since v0.97.0, and that is the whole point of
        the inversion.** The reported defect it fixes: *"if i switch tabs,
        the split out sessions go with the tab. Shouldn't the split out
        sessions be independent?"* -- Ctrl+←/→ cycles the tabs of the group
        holding the keyboard and leaves every other group exactly as it
        was."""
        group = self.focused_group()
        return group.tabs() if group is not None else []

    def _cycle_tab(self, delta: int) -> None:
        """Ctrl+← / Ctrl+→ -- move to the neighbouring tab, wrapping. One
        tab wraps to itself, which is the correct no-op.

        Focuses the tab it lands on, right here (v0.38.0). That used to be
        left to _on_tab_activated, one message-pump turn later -- and a
        pane mounting in the meantime could focus itself and take the
        activation back, which is exactly what made tests/test_tab_status.
        py's done-unseen test flaky after a Ctrl+T/Ctrl+← pair.

        :meth:`_cyclable_tabs`, not :meth:`panes` (v0.85.0 -- see that
        method's own docstring for the defect this fixes): a read-only
        tab has no prompt, so :meth:`_focus_tab` below is a no-op for one,
        same as it already is for a mouse click landing on one."""
        tabs = self._cyclable_tabs()
        if len(tabs) < 2:
            return
        tabbed = self._strip()
        ids = [t.id for t in tabs if t.id]
        try:
            index = ids.index(tabbed.active)
        except ValueError:
            index = 0
        tabbed.active = ids[(index + delta) % len(ids)]
        # Textual's reactive watcher has already moved the `-active` class
        # by the time that assignment returns, so the marker can be placed
        # NOW -- one message-pump turn earlier than TabActivated arrives.
        # Held Ctrl+←/→ is the case this exists for.
        self._jump_tab_marker()
        self._focus_active_tab()

    def _switch_to_tab(self, pane_id: str) -> None:
        """Take me to that pane, by id -- the palette's open-tab entries
        and a peer chip's jump to a session already open here. Same three
        beats as every other explicit switch: activate, move the marker,
        focus (v0.38.0).

        Accepts a LEAF id as well as a tab id (v0.91.0). Every caller
        passes a ``SessionPane``'s own id, and with splits that is no
        longer the same string as its tab's -- so this resolves the leaf,
        activates the tab that holds it, and lands the keyboard on THAT
        pane rather than on whichever leaf the tab was last in. Jumping to
        a peer and arriving at its neighbour would be the same defect as
        restoring onto the wrong tab, one level down."""
        leaf = next((p for p in self.panes() if p.id == pane_id), None)
        target_id = leaf.tab_id if leaf is not None else pane_id
        with contextlib.suppress(Exception):
            self._strip_for(target_id).active = target_id
        self._jump_tab_marker()
        if leaf is not None:
            self._focus_tab(leaf)
        else:
            self._focus_active_tab()

    async def _stop_active(self) -> None:
        """Palette 'Quit: stop session', tab-scoped: finalize the ACTIVE
        tab's session NOW; the tab closes with it. Stopping the only tab
        closes the app (the Phase 2 behavior, per-app == per-tab then).

        The palette's own name for Ctrl+Q -- v0.99.1 makes that literal by
        delegating to :meth:`_close_pane` (``terminate=True``) instead of
        re-deriving its disposition here a second time. Through v0.99.0
        this method reimplemented a SUBSET of that logic directly (no
        transcript-tab teardown, no split/group-aware removal via
        _close_group_tab, no is_last handling at all) and inherited none
        of _close_pane's fixes as a result -- stopping the ONLY tab from
        the palette left its session in the persisted record even after
        v0.85.0 taught Ctrl+Q's own path not to, and even after v0.99.1
        taught it that a stopped pane never belongs in the persisted set
        regardless of tab position. One implementation now, reached both
        ways."""
        pane = self.active_pane
        if pane is None:
            return
        await self._close_pane(pane, terminate=True)

# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.appwindow.actions -- what a key, or the palette, actually does.

The sixth and last of DoxaApp's method families to become a mixin, on the
terms the first five set: one family per module, same class, no behaviour
change. This one is the window's whole VERB surface -- every ``action_*``
Textual can dispatch to, the palette's own row list, and the ``_cmd_*``
adapters those rows call. It is the layer the other five are underneath:
almost nothing here does its own work, it decides WHICH of them to ask.

``BINDINGS`` deliberately does NOT come with it. The table is a class
attribute of the window, like ``CSS_PATH`` and ``DIVIDER_STEP`` beside it,
the suite reads it as ``DoxaApp.BINDINGS``, and it has to be declared in
the body Textual's ``_MessagePumpMeta`` constructs. It needs no change
either way: Textual resolves a binding by ``getattr(app, "action_<name>")``
at press time, which walks the MRO, so an ``action_*`` defined on a mixin
is found exactly as one defined on the class was.

The sidebar's own three. :meth:`WindowActionsMixin.action_toggle_sidebar`
is F3 -- the ``-- toggling`` banner in doxa/app.py, stated here because it
labelled only that one method -- and :meth:`action_sidebar_wider` /
:meth:`action_sidebar_narrower` are the width pair that sat in the middle
of the rail's six ``@on`` handlers. All three report a width refusal where
the user is looking rather than doing nothing.

Opening, splitting, diffing. :meth:`action_new_tab` is Ctrl+T, which was
the ``-- tab lifecycle`` banner; :meth:`action_split_pane` and
:meth:`action_vsplit_pane` were ``-- splits (v0.91.0)``, a second session
below or beside this one in the same tab; :meth:`action_toggle_diff` and
the four :meth:`action_focus_pane_left`-and-siblings were ``-- live diff
(v0.92.0)``. Each of the four banners labelled nothing that stayed.

Sizing and jumping. :meth:`action_divider_up` / :meth:`action_divider_down`
move the focused leaf's OWN status-bar divider and the four
:meth:`action_grow_pane_up`-and-siblings move the divider BETWEEN leaves;
their two banners stay in doxa/app.py, still labelling ``DIVIDER_STEP`` and
``GROUP_FLASH_SECS``. :meth:`action_focus_group` is ``Ctrl+<digit>``.

Closing, ending, cycling. :meth:`action_close_tab` (Ctrl+W) detaches and
:meth:`action_end_session` (Ctrl+Q) finalizes, which is the one distinction
those two keys exist to draw; :meth:`action_detach_tab` is the named form
of the first, :meth:`action_prev_tab` / :meth:`action_next_tab` walk the
strip, and :meth:`action_cycle_permission_mode` is Shift+Tab.

The palette, which was ``-- palette (Ctrl+P)``. :meth:`doxa_commands` is
the whole surface as :class:`~doxa.palette.PaletteEntry` rows, rebuilt from
live state on every open, and :meth:`action_command_palette` is the screen
it opens. The eight ``_cmd_*`` below it are what a row CALLS --
:meth:`_cmd_new_tab`, :meth:`_cmd_close_tab`, :meth:`_cmd_new_session`,
:meth:`_cmd_attach` with its :meth:`_cmd_attach_worker`,
:meth:`_cmd_stop_active`, :meth:`_cmd_run_slash` and :meth:`_cmd_prefill`
-- each one a worker dispatch or a prompt prefill, never a reimplementation
of the command itself. :meth:`action_history_search` (Ctrl+R),
:meth:`action_settings` (Ctrl+,), :meth:`action_setup` and
:meth:`action_toggle_inspector` are the modals the same rows reach.

Quitting, which was ``-- quit semantics (app-level, all tabs)``: neither
:meth:`action_quit_stop` nor :meth:`action_quit` lives on a key any more
(v0.85.0 dropped Ctrl+C -- see doxa/app.py's BINDINGS comment). Both stay
reachable from the palette ("Quit: detach" / "Quit: stop session") and
``action_quit_stop`` doubles as ``/update --restart``'s own shutdown path.

The forty move verbatim -- same order, same docstrings, same comments,
byte for byte -- after :mod:`doxa.appwindow.failures`,
:mod:`doxa.appwindow.restore`, :mod:`doxa.appwindow.sidebar`,
:mod:`doxa.appwindow.tabs` and :mod:`doxa.appwindow.panetree`. Not one
carries a decorator, and no ``@on`` handler could have come: Textual
registers handlers by scanning the class body it builds and never scans a
plain mixin's, which is why all sixteen are still in doxa/app.py --
fourteen of them INSIDE the span this family covers, from the rail's six
to the two the inspector and the chips own. Three deferred imports were
re-levelled a package up -- ``..engine``'s ``next_cycle_mode``,
``..settings``'s ``SettingsScreen`` and ``..setup``'s ``SetupScreen`` --
and every other moved line is identical to the character.

No constant moved and nothing is re-exported. Nothing the suite patches on
``doxa.app`` is read from here either: ``SessionEngine`` (PEP 562),
``_stop_session``, ``ChipPicker`` and ``ReasoningSection`` appear in none
of the forty, and the two names that DO appear -- ``notify_mod``, which the
suite patches as ``doxa.app.notify_mod.notify``, and ``ClockChip``, which
it only imports -- are a module object and a class object, the same ones
whichever module names them.
"""

from __future__ import annotations

import contextlib
from functools import partial

from textual.app import App

from .. import commands as commands_mod
from .. import config as config_mod
from .. import layout as layout_mod
from .. import notify as notify_mod
from .. import palette as palette_mod
from .. import peers as peers_mod
from ..history import SEARCH_PREFIX
from ..palette import PaletteEntry
from ..ui.dialogs import BeliefInspector
from ..ui.prompt import PromptInput
from ..ui.statusline import ClockChip


class WindowActionsMixin:
    """DoxaApp's verb half. Mixed into the app, never used standalone:
    every method here reaches the rest of the window through ``self`` --
    the strips and the panes it asks the pane tree for, the tabs it opens
    and closes, the rail it widens, and the sessions the peer registry
    says are attachable."""

    # -- the mesh graph server (one per window, never per pane) --------
    #
    # A PORT IS PROCESS-WIDE, so the server is too: two panes that each
    # started one would bind two loopback ports over the same ledger and
    # print two URLs, and the status chip -- which every pane in the
    # window paints -- could then only be honest about one of them. The
    # handle therefore lives on the window and every /mesh goes through
    # the three methods below.

    def mesh_server(self) -> "object | None":
        """The running :class:`doxa.meshgraph.MeshServer`, or None.

        ``getattr`` rather than an ``__init__`` default: this mixin has no
        constructor of its own (the window's is in doxa/app.py) and adding
        one to carry a field that is None on every launch but the ones
        where somebody typed /mesh would put the attribute before the
        need."""
        return getattr(self, "_mesh_server", None)

    def mesh_url_for(self, path: "object | None" = None) -> str:
        """The URL, but only if the running server is serving ``path``.

        The fleet tab asks with its own run's ledger, which is the whole
        point: a mesh started over THIS machine's peer ledger is not a
        view of that run, and a tab that printed its URL anyway would be
        pointing an operator at the wrong graph. ``None`` asks for the
        URL whatever it is serving, which is what the status chip and
        /mesh's own reply want."""
        from pathlib import Path

        server = self.mesh_server()
        if server is None:
            return ""
        if path is not None and Path(str(path)) != Path(str(server.path)):
            return ""
        return str(server.url)

    def start_mesh(self, path: "object") -> "tuple[object | None, str]":
        """Start the loopback graph server over ``path``. Returns
        ``(server, note)``; ``server`` is None when nothing started.

        Refuses to start a SECOND one rather than replacing the first: the
        running server holds a port and a token the operator may already
        have open in a browser, and silently swapping the file underneath
        that URL is worse than saying no and naming ``/mesh stop``."""
        from pathlib import Path

        from .. import meshgraph as meshgraph_mod

        running = self.mesh_server()
        if running is not None:
            same = Path(str(running.path)) == Path(str(path))
            return running, (
                f"mesh is already up on {running.url}"
                + ("" if same else f" over {running.path} — /mesh stop first")
            )
        try:
            server = meshgraph_mod.MeshServer(path=Path(str(path)))
        except Exception as exc:  # noqa: BLE001 -- a bind failure is a
            # message, not a crash: the port may be taken, and the caller
            # is a keystroke.
            return None, f"mesh could not start: {type(exc).__name__}: {exc}"
        self._mesh_server = server
        for pane in self.panes():
            with contextlib.suppress(Exception):
                pane._refresh_status()
        return server, ""

    def stop_mesh(self) -> bool:
        """Stop it and drop the handle. True when one was running."""
        server = self.mesh_server()
        self._mesh_server = None
        if server is None:
            return False
        with contextlib.suppress(Exception):
            server.stop()
        for pane in self.panes():
            with contextlib.suppress(Exception):
                pane._refresh_status()
        return True

    def on_unmount(self) -> None:
        """The window is going away: release the mesh port.

        BOTH here and in :meth:`doxa.app.DoxaApp.run`'s own ``finally``,
        because neither alone covers every exit -- doxa/app.py's ``run``
        docstring already records that ``on_unmount`` does not fire on
        every way out of a TUI, and ``run()`` is not the door
        ``App.run_test`` comes through. :meth:`stop_mesh` is idempotent,
        so being called twice is not a state anybody has to reason
        about."""
        with contextlib.suppress(Exception):
            self.stop_mesh()

    def action_toggle_sidebar(self) -> None:
        """F3. Reports a width refusal where the user is looking --
        the active pane's transcript -- rather than doing nothing, which
        is the "documented key that silently does nothing" failure
        v0.39.0 exists to prevent."""
        rail = self.sidebar()
        showing = bool(rail is not None and rail.styles.display != "none")
        note = self.set_sidebar(not showing)
        if note:
            self.notify_sidebar(note)

    def action_sidebar_wider(self) -> None:
        self._nudge_sidebar(1)

    def action_sidebar_narrower(self) -> None:
        self._nudge_sidebar(-1)

    async def action_new_tab(self) -> None:
        """Ctrl+T: a fresh session in the same repo scope (exactly
        new_session_factory -- a new daemon under the CLI, a new in-process
        engine otherwise), attached in a new tab and focused.

        All three steps are stated here, in order: mount, activate, focus
        -- and then PERSIST, which is the fourth (v0.91.0). Focus used to
        arrive on its own, from the pane's own mount, and activation used
        to arrive as a side effect of THAT -- so the keystroke's outcome
        was really a race with Textual's mount scheduling (v0.38.0).

        The explicit persist closes the last thread of that same race, at
        the other end: a pane whose engine answers instantly (every
        FakeEngine in the suite, and a warm daemon reattach) can finish
        booting INSIDE the ``add_pane`` await, and ``_note_pane_booted``
        then writes the tab set before the next two lines have said which
        tab is active. Nothing wrote it again, so the record kept naming
        the tab the user came from -- measured as a real failure of
        tests/test_tabsets.py's own append test under a full-suite run,
        and not reproducible on its own."""
        tabbed = self._strip()
        pane = self._make_pane(self._new_session_factory)
        tab = self._make_tab(pane)
        await tabbed.add_pane(tab)
        self._activate_tab(tab)
        self._focus_tab(tab)
        self._persist_tabset()

    async def action_split_pane(self) -> None:
        """Ctrl+O (Alt+S under the kitty protocol) / ``/split`` -- a fresh
        session STACKED BELOW this
        one, in the same tab. vim's sense of the word, which is the sense
        ``/split`` has always had here."""
        note = await self.split_active_pane(layout_mod.COLUMN)
        if note:
            self.notify(note, severity="warning", timeout=8)

    async def action_vsplit_pane(self) -> None:
        """Ctrl+N (Alt+D under the kitty protocol) / ``/vsplit`` -- a fresh
        session SIDE BY SIDE with
        this one, in the same tab."""
        note = await self.split_active_pane(layout_mod.ROW)
        if note:
            self.notify(note, severity="warning", timeout=8)

    async def action_toggle_diff(self) -> None:
        """F2 (Alt+G under the kitty protocol) / ``/diff``."""
        note = await self.toggle_diff_pane()
        if note:
            self.notify(note, severity="warning", timeout=8)

    def action_focus_pane_left(self) -> None:
        self.focus_pane_towards("left")

    def action_focus_pane_right(self) -> None:
        self.focus_pane_towards("right")

    def action_focus_pane_up(self) -> None:
        self.focus_pane_towards("up")

    def action_focus_pane_down(self) -> None:
        self.focus_pane_towards("down")

    def action_divider_up(self) -> None:
        """Ctrl+Up: grow the transcript, shrink the prompt area.

        Acts on the FOCUSED leaf's own status-bar divider -- each leaf has
        one, and that is how the spec resolves "Ctrl+Up/Down cannot mean
        two things" once splits exist. The divider BETWEEN leaves has its
        own gesture (Alt+arrow, :meth:`grow_pane_towards`) rather than
        being silently overloaded onto this pair."""
        pane = self.active_pane
        if pane is not None and pane.nudge_prompt(-1):
            self._persist_tabset()

    def action_divider_down(self) -> None:
        """Ctrl+Down: grow the prompt area, shrink the transcript."""
        pane = self.active_pane
        if pane is not None and pane.nudge_prompt(1):
            self._persist_tabset()

    def action_focus_group(self, number: int) -> None:
        """``Ctrl+<digit>`` -- put the keyboard in the group at that
        position, and flash every group's number.

        Both, always, and in that order. The jump happens IMMEDIATELY: the
        overlay is feedback and teaching, not a mode, and DOXA does not
        wait for a second keystroke the way tmux's ``display-panes`` does,
        because the numbering is meant to become muscle memory and a
        prompt-then-wait gesture never lets it.

        The flash fires even when the digit names NO group -- pressing
        Ctrl+7 in a two-group layout shows 1 and 2 and moves nothing. That
        is the case it earns the most in: it answers "what are my choices"
        for a user who guessed."""
        groups = self._group_order()
        self._flash_group_numbers()
        if 1 <= number <= len(groups):
            target = groups[number - 1]
            surface = next(iter(target.surfaces()), None)
            if surface is not None:
                self._focus_tab(surface)

    def action_grow_pane_up(self) -> None:
        self.grow_pane_towards("up")

    def action_grow_pane_down(self) -> None:
        self.grow_pane_towards("down")

    def action_grow_pane_left(self) -> None:
        self.grow_pane_towards("left")

    def action_grow_pane_right(self) -> None:
        self.grow_pane_towards("right")

    async def action_close_tab(self) -> None:
        """Ctrl+W: close-DETACH the active tab -- its daemon keeps running,
        by design (reattach via the palette's attach picker or `doxa
        attach`). The cheapest outcome to recover from is what a close key
        does; ENDING a session is Ctrl+Q, which says so.

        The three non-session tabs take the SAME key to a much simpler
        path -- :meth:`_close_read_only_tab`, which Ctrl+Q now shares.

        Closing the last SESSION tab closes the app, on the same detach
        semantics."""
        pane = self.active_pane
        if pane is not None:
            await self._close_pane(pane, terminate=False)
            return
        await self._close_read_only_tab()

    def action_end_session(self) -> None:
        """Ctrl+Q: END this tab's session -- finalize NOW (LORE review +
        index run daemon-side), socket closed, presence file removed, the
        daemon child reaped -- and close the tab. Nothing survives but the
        transcript.

        Tab-scoped, never app-scoped: quitting the whole window lives on
        the command palette ("Quit: detach" / "Quit: stop session"), not
        on this key. A turn IN FLIGHT is the one case this refuses to
        decide by itself -- killing work silently is not a thing a
        keystroke should do -- so it asks; an idle session ends without a
        prompt.

        On a tab with NO session to end -- a subagent transcript, a
        restored archive -- it closes the tab, and that is the whole of
        what it does. Through v0.56.0 it did NOTHING there: ``_end_session``
        looked for a SessionPane, found None and returned, so the user sat
        on a read-only tab pressing the key they had been taught closes
        tabs. Same defect class as the (then still shipping) beliefs
        browser and Ctrl+W in v0.46.0, and it now takes the same shared
        answer, :meth:`_close_read_only_tab`.

        That does NOT make the two keys the same key. The distinction is
        about the SESSION -- Ctrl+W leaves it running, Ctrl+Q finalizes it
        -- and on a tab with no session there is no distinction left to
        draw: the archive's session ended before the window opened, the
        subagent's transcript is a copy. Two keys agreeing where the
        difference is meaningless is not ambiguity, it is the absence of a
        trap. What would be wrong is Ctrl+Q ending the tab's OWNING
        session -- a key aimed at the
        visible tab must never reach past it -- and it does not.

        Dispatched into a worker because awaiting a modal's answer
        (push_screen_wait) is only legal from one."""
        self.run_worker(self._end_session(), group="close")

    async def action_detach_tab(self) -> None:
        """`/detach` -- the named form of what Ctrl+W does."""
        await self.action_close_tab()

    def action_prev_tab(self) -> None:
        self._cycle_tab(-1)

    def action_next_tab(self) -> None:
        self._cycle_tab(1)

    def action_cycle_permission_mode(self) -> None:
        """Shift+Tab (and Ctrl+Tab where the terminal can send it): step
        the ACTIVE pane's session to the next permission mode.

        The key can only ever reach this SESSION's own ring --
        default → acceptEdits → plan → default. That is not a check
        performed here; it is a property of
        :func:`doxa.engine.next_cycle_mode`, which is total over that
        tuple and cannot return anything outside it whatever this pane's
        current mode happens to be. Putting the boundary in a pure
        function rather than in this handler is what makes it testable as
        a security assertion instead of as a UI behavior.

        A session parked on a gated mode (reached through ``/mode`` and a
        confirmation) is off the ring, so one press brings it home to
        ``default`` -- which is also the one thing a user reaching for a
        key to get out of bypass would want it to do.

        Dispatch goes through ``_cmd_mode`` like every other door, so the
        transcript records the switch in the same words a typed
        ``/mode`` would, and there is exactly one place that talks to the
        engine."""
        from ..engine import next_cycle_mode

        pane = self.active_pane
        if pane is None or pane.engine is None:
            return
        # The ring is per-session since v0.58.0: a session not spawned
        # with the arming flag has no bypassPermissions in it, so the key
        # steps straight from auto back to default rather than offering a
        # mode the CLI would refuse.
        target = next_cycle_mode(
            getattr(pane.engine, "permission_mode", None),
            bool(getattr(pane.engine, "bypass_armed", False)),
        )
        pane.run_worker(pane._cmd_mode(target), group="command")

    def doxa_commands(self) -> "list[PaletteEntry]":
        """The DOXA palette surface, as :class:`~doxa.palette.PaletteEntry`
        rows in display order.

        Rebuilt from live state on EVERY palette open (that is what the
        provider calls), so a tab opened or closed while the palette is up
        cannot leave a stale row behind.

        The order is the one doxa/palette.py documents: New tab, then the
        open tabs in tab-bar order, then the commands in the registry's own
        groups, then the attachable sessions. App-level entries that have
        no registry row (Close tab, the quits, the inspector) declare a
        registry GROUP like everything else -- there is one grouping in
        this app, not one per surface."""
        entries: list[PaletteEntry] = [
            PaletteEntry(
                palette_mod.SECTION_NEW,
                "New tab",
                "Open a fresh DOXA session in this repo scope in a new tab (ctrl+t)",
                self._cmd_new_tab,
            ),
        ]
        # Open tabs, LEFT TO RIGHT -- the palette mirrors the tab bar, so
        # the order the user sees along the top is the order they get here.
        # The active tab is marked rather than hidden: "where am I" is as
        # much a question as "where do I want to go".
        active = self.active_pane
        for position, pane in enumerate(self.panes()):
            if not pane.id:
                continue
            sid = str(getattr(pane.engine, "session_id", "") or "")[:8]
            is_active = pane is active
            entries.append(PaletteEntry(
                palette_mod.SECTION_TABS,
                # ``display_name()``, not the TabPane ``_title`` this read
                # through v0.88.0: a leaf is no longer the tab, and with
                # two sessions in one tab the header's title names only
                # the first of them. This names THIS pane -- which is
                # also what makes the palette the place two panes sharing
                # a tab are told apart, alongside the session id below.
                f"{pane.display_name()}" + (f"  ({sid})" if sid else "")
                + ("  · active" if is_active else ""),
                "This tab (already active)" if is_active
                else "Switch to this tab",
                partial(self._switch_to_tab, pane.id),
                sort_key=(position, ""),
            ))
        # App-level entries: no slash row of their own, but the SAME
        # registry groups -- they sort after the registry's rows inside a
        # group (sort_key (1, label) vs the registry's (0, name)).
        for group, label, help_text, callback in (
            ("Panes & tabs", "Close tab",
             "Close-detach the current tab; its session keeps running (ctrl+w)",
             self._cmd_close_tab),
            ("Session", "New session",
             "Start a fresh DOXA session and switch THIS tab to it",
             self._cmd_new_session),
            ("Panes & tabs", "Belief inspector: toggle",
             "Show/hide the belief inspector pane (stub until Phase 3)",
             self.action_toggle_inspector),
            ("Session", "Quit: detach",
             "Close this TUI; every session daemon keeps running "
             "(reattach with `doxa attach`)",
             self.action_quit),
            ("Session", "Quit: stop session",
             "Finalize the current tab's session now (LORE review + index) "
             "and close its tab",
             self._cmd_stop_active),
        ):
            entries.append(PaletteEntry(group, label, help_text, callback))
        # Slash registry, second surface: every row that declares a palette
        # label appears here too (doxa/commands.py is the single list --
        # the prompt's autocomplete reads the same rows), keeping
        # commands.ordered()'s sequence inside its group. Rows that need
        # arguments PREFILL the prompt instead of running blind.
        for index, command in enumerate(commands_mod.ordered()):
            if not command.palette:
                continue
            callback = (
                partial(self._cmd_prefill, command.name + " ")
                if command.palette_prefill
                else partial(self._cmd_run_slash, command.name)
            )
            entries.append(PaletteEntry(
                command.group, command.palette, command.summary, callback,
                sort_key=(0, f"{index:03d}"),
            ))
            # A row's own verbs, directly under it and in its own group --
            # never a second ordering (doxa/palette.py). The sort key
            # extends the parent's rather than replacing it, so "012.00"
            # falls between "012" and "013" by plain string order and a
            # verb can never drift away from the command it belongs to.
            for position, sub in enumerate(command.subcommands):
                line = f"{command.name} {sub.argument}"
                entries.append(PaletteEntry(
                    command.group, sub.palette, sub.summary,
                    partial(self._cmd_prefill, line + " ") if sub.prefill
                    else partial(self._cmd_run_slash, line),
                    sort_key=(0, f"{index:03d}.{position:02d}"),
                ))
        # Attach: live daemon-hosted sessions from the shared peer/daemon
        # registry, newest first, never any session already open in a tab.
        open_ids = {
            str(getattr(p.engine, "session_id", "") or "") for p in self.panes()
        }
        for position, entry in enumerate(peers_mod.list_daemons()):
            if entry.session_id in open_ids:
                continue
            entries.append(PaletteEntry(
                palette_mod.SECTION_ATTACH,
                f"Attach: {entry.title} ({entry.session_id[:8]})",
                f"Reattach to the live session in {entry.cwd} (in this tab)",
                partial(self._cmd_attach, entry),
                sort_key=(position, ""),
            ))
        return palette_mod.ordered_entries(entries)

    def action_command_palette(self) -> None:
        """Ctrl+P -- DOXA's palette screen, which is Textual's plus the
        section headers (doxa/palette.py). Overridden rather than
        configured because Textual's App pushes its own CommandPalette
        class by name."""
        from textual.command import CommandPalette

        if self.use_command_palette and not CommandPalette.is_open(self):
            self.push_screen(palette_mod.DoxaPalette(id="--command-palette"))

    def _cmd_new_tab(self) -> None:
        self.run_worker(self.action_new_tab(), group="tabs")

    def _cmd_close_tab(self) -> None:
        self.run_worker(self.action_close_tab(), group="tabs")

    def _cmd_new_session(self) -> None:
        pane = self.active_pane
        if pane is not None:
            pane.run_worker(
                pane.switch_engine(self._new_session_factory),
                exclusive=True, group="switch",
            )

    def _cmd_attach(self, entry: peers_mod.PeerInfo) -> None:
        """The palette's "Attach: ..." entries and the sessions chip's own
        picker (:meth:`doxa.session.chips.PaneChipsMixin._select_session_row`)
        both land here -- attach to a live, DETACHED daemon session, in a
        NEW tab, through :meth:`_attach_in_new_tab` (the same door /resume
        already sends a still-running session through).

        v0.60.0, reported and MEASURED, not assumed: through v0.56.0 this
        switched the ACTIVE pane's engine in place instead (item 2's own
        original spec, two releases before /resume settled "a pane holds
        a live conversation; attaching is never a takeover"). Driven end
        to end against a real SessionDaemon over a real socket before this
        changed: the connection itself worked (the pane's engine really
        did become an EngineClient for the right session id) -- what did
        not was the CONTENT. switch_engine() never sets
        _restore_transcript_wanted, so a reattached pane's history came
        from the daemon's in-memory event ring alone (the pre-v0.32.0
        mechanism, capped at 512 frames -- see SessionPane._restore_
        transcript's own docstring for the exact defect that capacity
        already caused once). A session detached long enough to have
        scrolled its ring past that came back BLANK, in the tab the user
        was already looking at -- which is indistinguishable from
        "nothing happened" even though a socket really did connect.
        _attach_in_new_tab sets that flag and opens a tab with nothing
        else in it to confuse the result with.

        Both callers already exclude a session open in ANOTHER tab of
        this window from their own candidate list before this is ever
        reached (the palette's own Attach section, and _select_session_
        row's separate switch-instead branch above it) -- this is the one
        attach primitive, never re-derives that exclusion."""
        self.run_worker(self._cmd_attach_worker(entry), group="switch")

    async def _cmd_attach_worker(self, entry: "peers_mod.PeerInfo") -> None:
        note = await self._attach_in_new_tab(entry.session_id, entry.title)
        if note:
            # App-scoped, not pane-scoped: unlike /attach (typed IN a
            # pane, which can print its own note as a SystemBlock there),
            # this is reached from the palette and the sessions chip alike
            # with no "the pane this is about" to write into -- a toast is
            # the one surface both share.
            self.notify(note, severity="information", timeout=10)

    def _cmd_stop_active(self) -> None:
        self.run_worker(self._stop_active(), group="tabs")

    def _cmd_run_slash(self, name: str) -> None:
        """Palette -> the ACTIVE pane's slash handler. One dispatch path for
        both surfaces: the palette never reimplements a command.

        ``active_pane`` is SessionPane-only and is therefore None whenever
        a READ-ONLY tab is the active one -- an archived transcript, a
        subagent's activity, a fleet run. Through v1.14.0 that made every
        palette command a no-op in those tabs, which is the "documented
        action that silently does nothing" failure this house treats as a
        defect rather than a rough edge; the two tab kinds that know which
        pane they belong to (``owner``) supply one, so "Fleet: status"
        works from the tab the run is in."""
        pane = self.active_pane or self._owner_of_active_tab()
        if pane is not None:
            pane.run_worker(pane._run_command(name), group="command")

    def _owner_of_active_tab(self) -> "object | None":
        """The SessionPane a read-only active tab belongs to, or None.

        Reads ``owner`` off whatever tab is active rather than asking each
        tab kind by type: a tab that declares an owner is declaring that a
        command typed "here" means that pane, and a tab that does not
        (an archived transcript -- its session is gone) truthfully has
        nobody to answer for it."""
        from ..session.pane import SessionPane

        active: "object | None" = None
        with contextlib.suppress(Exception):
            active = self._strip().active_pane
        owner = getattr(active, "owner", None)
        return owner if isinstance(owner, SessionPane) else None

    def _cmd_prefill(self, text: str) -> None:
        pane = self.active_pane
        if pane is None:
            return
        prompt = pane.query_one("#prompt-input", PromptInput)
        prompt.value = text  # the setter also moves the cursor to the end
        prompt.focus()

    def action_history_search(self) -> None:
        """Ctrl+R: prefill ``/search `` in the active tab's prompt, which
        IS the search surface (doxa/history.py's popup opens on that exact
        prefix). The modal overlay this used to push is gone: one key, one
        slash command and one palette entry now land on the same place, so
        there is nothing left for two search paths to disagree about."""
        self._cmd_prefill(SEARCH_PREFIX)

    def action_settings(self) -> None:
        """Ctrl+, / /settings / the palette's Settings entry -- one modal,
        three doors. Saving re-reads the affected surfaces immediately
        (the status line's branch glyph and the plan chip are the two that
        show without a new session); knobs the ENGINE reads take effect on
        its next read, which is per turn by construction."""
        from ..settings import SettingsScreen

        def _saved(saved: "bool | None") -> None:
            if not saved:
                return
            config_mod.invalidate()
            notify_mod.sync_lore_notify_env()
            for pane in self.panes():
                pane._refresh_status()
            with contextlib.suppress(Exception):
                self.query_one(ClockChip).reconfigure()
            self._apply_background()
            self.refresh_css(animate=False)  # re-reads $doxa-base -- live

        engine = self.engine
        self.push_screen(
            SettingsScreen(
                session_model=getattr(engine, "model", None),
                account=getattr(engine, "account", None) or {},
            ),
            callback=_saved,
        )

    def action_setup(self) -> None:
        """/setup / the palette's Setup entry -- check state, fix findings
        one at a time. Also what a genuine first launch auto-triggers (see
        on_mount): the marker that stops it recurring is consumed there,
        not here, so this method itself is identical whether it was
        summoned on demand or by the app."""
        from ..setup import ACTION_OPEN_SETTINGS, SetupScreen

        def _done(result: "str | None") -> None:
            config_mod.invalidate()
            notify_mod.sync_lore_notify_env()
            for pane in self.panes():
                pane._refresh_status()
            if result == ACTION_OPEN_SETTINGS:
                self.action_settings()

        self.push_screen(SetupScreen(), callback=_done)

    def action_toggle_inspector(self) -> None:
        """Belief-inspector stub: Phase 3 owns the real pane (live STEER/
        CITE split, evidence trails); Phase 2 reserves the toggle, the dock
        and the count so the palette command and the muscle memory exist."""
        panel = self.query_one("#belief-inspector", BeliefInspector)
        if panel.display:
            panel.display = False
            return
        beliefs = self.engine.belief_count() if self.engine is not None else 0
        panel.set_text(
            f"{beliefs} active beliefs in the store.\n\n"
            "Phase 3 renders them here: STEER/CITE split,\n"
            "evidence trails, calibration. Until then use\n"
            "the lore_belief_search / lore_belief_show tools."
        )
        panel.display = True

    async def action_quit_stop(self) -> None:
        """Quit-stop, ALL tabs -- finalize every session NOW. Over a daemon
        client this stops the daemon itself (LORE review + index run
        there); in-process it is plain finalize-and-quit.

        A pane the user DETACHED on purpose is not stopped: detaching is
        the explicit "keep this running" gesture, and a later quit must not
        quietly undo it. Those sessions outlive the window, which is what
        /sessions exists to show and reap."""
        for pane in self.panes():
            if pane.detached_on_purpose:
                await pane.detach()
            else:
                note = await pane.stop()
                if note:
                    # Best-effort: the app quits right after this loop, so
                    # this toast may not get a paint frame -- the daemon's
                    # own log line (doxa.daemon._finalize_worktree) is the
                    # channel actually guaranteed to survive quitting the
                    # TUI, exactly the "headless" case worktrees.finalize's
                    # docstring calls out.
                    self.notify(note, severity="information", timeout=10)
        # Item D: one snapshot after the loop -- every pane above is still
        # MOUNTED (detached or stop()-marked _stopped, neither removed:
        # the app quits right below), so _persist_tabset's own per-pane
        # scan reads every one of them without help from either side dict.
        # A stopped pane is excluded there again as of v0.99.1 (see that
        # method's own docstring for the v0.60.0 detour) -- so this method
        # (palette 'Quit: stop session', all tabs -- Ctrl+C used to reach
        # it too, through v0.84.0) now matches ending them one at a time
        # with Ctrl+Q exactly: a detached pane is still written (item D
        # #4, unchanged), a stopped one is not. Nothing special had to
        # change HERE for that to be true -- the mounted-pane scan this
        # reads from is the one and only choke point, which is the point.
        self._persist_tabset()
        self.stop_mesh()
        await App.action_quit(self)

    async def action_quit(self) -> None:
        """palette 'Quit: detach' -- ALL tabs. Over a daemon client,
        finalize() only DETACHES: the daemon lingers and runs the
        session-end review + index itself once the last client has been
        gone for the linger window (or on `doxa stop`). In-process (Phase
        1 shape), finalize() still runs the review + index right here,
        host-driven (PHASE0 redesign item 1: no SessionEnd hook
        exists)."""
        for pane in self.panes():
            await pane.detach()
        # Item D: every pane stays mounted here (detach() only clears the
        # engine handle) -- the snapshot picks all of them up on its own.
        self._persist_tabset()
        self.stop_mesh()
        await App.action_quit(self)

"""doxa.session.pane -- SessionPane itself: state, layout, key routing.

What is left of the class after :mod:`doxa.session.commands`,
:mod:`doxa.session.chips` and :mod:`doxa.session.runtime` took their
thirds: the constructor and the per-session state it names, ``compose``
(the widget subtree one tab owns), the tab-label/tab-status writers, and
every message handler.

The handlers are HERE and not in a mixin for a mechanical reason, not a
stylistic one: Textual's ``MessagePumpMeta`` collects ``@on``-decorated
handlers out of the class body it is constructing, so a decorated handler
written in a plain mixin would never be dispatched. Handlers found by
naming convention work from anywhere in the MRO, but keeping all of them
in one place means the rule is "handlers live in pane.py" rather than "some
handlers live in pane.py".
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any, Callable  # noqa: F401 -- annotation-only

from textual import events, on
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import OptionList, TabbedContent, TabPane, TextArea

from .. import commands as commands_mod
from .. import naming as naming_mod
from .. import shell as shell_mod
from .. import providers as providers_mod
from .. import transcript as transcript_mod
from ..history import SessionSearch
from ..shell import SHELL_PREFIX
from ..ui.dialogs import ChipPicker, NeedsInputPopup, SlashComplete
from ..ui.labels import (
    _write_tab_class,
    compose_tab_label,
    ellipsize,
    provider_glyph,
    short_model,
)
from ..ui.prompt import PromptInput
from ..ui.statusline import GitLine, StatusBar
from ..ui.transcript import (
    ShellBlock,
    SubagentLine,
    SubagentTranscriptTab,
    SystemBlock,
    ToolChip,
    TurnBlock,
    mount_transcript,
)
from .chips import PaneChipsMixin
from .commands import PaneCommandsMixin
from .runtime import PaneRuntimeMixin


class SessionPane(PaneCommandsMixin, PaneChipsMixin, PaneRuntimeMixin, TabPane):
    """One session's whole surface: engine handle, block list, status bar,
    prompt input, and the boot/pump workers that drive them.

    This is the README sketch's extraction step: exactly the widget subtree
    (and exactly the per-session state) the single-pane app owned before,
    now owned per tab. Every worker this pane starts runs on the PANE node
    (``self.run_worker``), so exclusivity groups are scoped per tab and a
    removed tab takes its workers down with it (Textual cancels a node's
    workers on removal).

    v0.34.0 split the BODY of this class across three mixins (see
    :mod:`doxa.session`); the class, its name, its bases as Textual's CSS
    sees them, and every method's behaviour are unchanged."""

    def __init__(
        self,
        title: str,
        cwd: str,
        model: str | None,
        engine_factory: "Callable[[], Any]",
        *,
        id: str | None = None,
    ) -> None:
        super().__init__(title, id=id)
        self.cwd = cwd
        self.model = model
        self.engine: Any | None = None
        self._engine_factory = engine_factory
        self._engine_ready = asyncio.Event()
        # Item D (tab restore): the session id this pane's boot() last
        # reported, cached OUTSIDE self.engine so it survives detach()/
        # stop() clearing that handle -- _persist_tabset reads THIS, never
        # `self.engine.session_id`, so a just-detached tab still persists
        # under the right id. "" until the first boot completes.
        self._session_id: str = ""
        # Set True at the top of stop() -- a session that was EXPLICITLY
        # ended must never reappear in the persisted tab set, even in the
        # brief window before its pane is actually unmounted (see
        # DoxaApp._persist_tabset).
        self._stopped: bool = False
        # Item D restore-only: a pinned name to apply the moment this pane
        # mounts (before boot), and a one-shot SystemBlock to mount right
        # after the identity block on its first boot ("restored N tabs,
        # skipped M"). Both None for every ordinarily-created tab.
        self._initial_pinned_name: "str | None" = None
        self._boot_report: "str | None" = None
        # v0.32.0 restore-only: put this session's PRIOR CONVERSATION back
        # on screen. True means _boot renders doxa.transcript's reading of
        # the session's persisted transcript before handing over to the
        # live stream -- see _restore_transcript for why that is disk and
        # not the daemon's replay ring. False (every ordinary tab, and a
        # restore whose daemon is too old to let us skip its backlog)
        # leaves v0.31.0's replay-only behavior exactly as it was.
        self._restore_transcript_wanted = False
        # The session's OWN cwd, from the saved tab record -- the engine's
        # reported cwd wins once it boots, this is the before-boot answer
        # (and the ONLY answer an archived tab ever has).
        self._restore_cwd: "str | None" = None
        # NOTE (v0.38.0): a pane does NOT decide its own focus. It used to
        # -- on_mount focused this pane's prompt, guarded by a
        # _focus_on_mount flag -- and because focusing a widget inside a
        # TabPane ACTIVATES that pane (TabbedContent._on_tab_pane_focused),
        # that made ACTIVATION a side effect of mounting, arriving whenever
        # Textual got round to it. Focus now belongs to DoxaApp, at each
        # site that moves the user on purpose (DoxaApp._focus_tab and its
        # callers). Nothing to store here any more.
        # Out-of-band turn rendering state (replayed history after reattach,
        # or a turn another attached client drives) -- see _peer_pump.
        self._oob_turn: TurnBlock | None = None
        self._oob_chips: dict[str, ToolChip] = {}
        # Status-line git chip -- built in _boot (per engine, since attach
        # can land in another project's cwd), refreshed event-driven only.
        self._git: GitLine | None = None
        # Tab label: `<short model> · <repo> ⎇ <branch>`, recomputed from
        # the tracked model and the (event-driven) GitLine wherever the
        # status bar is refreshed -- never on a timer, and only WRITTEN
        # when it actually changed, since writing it repaints the tab.
        self._tab_label: str | None = None
        # A tab the user NAMED. Set, it pins the label: model switches and
        # branch changes stop rewriting it, because a name the user typed
        # outranks anything DOXA can derive. Cleared (an empty rename) the
        # automatic label takes over again -- that is the only un-pin.
        self.custom_name: str | None = None
        # Outside a repo there is no repo:branch to label with, so the tab
        # is named from the session's first turn (doxa/naming.py, one Haiku
        # call, cached). None until that lands -- the dirname stands in
        # meanwhile, and a failure leaves it standing for good.
        self.generated_name: str | None = None
        self._naming_done = False
        # Is a turn running right now? Ctrl+W asks before killing one.
        self.turn_in_flight = False
        # Did the USER detach this session on purpose? Then it is no longer
        # this window's to terminate -- quit-stop leaves it alone, and
        # /sessions' kill-all-detached is the only thing that comes for it.
        self.detached_on_purpose = False
        # Subscription-headroom chip, recomputed at most once per turn-done
        # (see _refresh_usage_chip). Cached as a plain string because
        # _refresh_status runs on every peer event and must stay free.
        self._usage_chip: str | None = None
        # Attention-blink infra (tab status, item: per-status tab colors).
        # Nothing sets this True yet -- the engine-side event that should
        # (can_use_tool / AskUserQuestion plumbing, a session waiting on the
        # user mid-turn) is phase 2. What exists here is the mechanism: a
        # timer that blinks the -attention class on the tab, alive ONLY
        # while needs_input is True (see set_needs_input) -- this app
        # measures idle CPU and a timer nothing ever stops is exactly the
        # busy-idle bug GitLine's docstring warns about, reintroduced.
        self.needs_input = False
        self._attention_timer: Any = None
        self._attention_on = False
        # Staged-proposal signal (v0.31.0): the streaming deriver extracted
        # something and did not reject it. A SEPARATE tab class from the
        # three above, written through the same _set_tab_class door -- see
        # set_staged for why it is a steady tint and never a blink.
        self.staged_pending = False
        # How many proposals LORE has staged, counted ONCE at boot for the
        # opening block's `lore` line (v0.51.0 -- see _lore_memory_bits).
        # None means "not asked yet, or the engine could not answer", which
        # that line renders as omission rather than as a zero nobody
        # measured. Never refreshed on a status tick: the count costs a
        # socket round trip to the daemon, and _refresh_status runs under
        # the no-timer, no-per-frame rule GitLine documents.
        self._pending_count: int | None = None
        # Subagent tracker (queue item 4): running Task-spawned subagents
        # for THIS pane, tool_use_id -> the ToolChip already mounted in the
        # trace tree -- a second INDEX into that same widget, not a copy of
        # its state. Entries exist ONLY while running (added on a top-level
        # or nested tool_call named "Task", popped on that same id's own
        # tool_result), so len() IS the live count the status chip and the
        # second line both read, arrival order (plain dict insertion order)
        # is all either needs, and no wall clock is kept anywhere.
        self._subagents: dict[str, ToolChip] = {}
        # Open transcript tabs for THIS pane's subagents, call_id -> tab.
        # Outlives the matching _subagents entry (a finished subagent's tab
        # stays open, marked done, until the user closes it) but never
        # outlives the tab itself -- popped in _close_transcript_tab.
        self._transcript_tabs: dict[str, SubagentTranscriptTab] = {}
        # Item V: this pane's beliefs browser tab, or None when it has
        # never been opened (or was closed). One per pane -- reopening
        # activates this one rather than stacking a second, and the
        # is_mounted re-check in open_beliefs_browser is what notices a
        # tab the user closed behind DOXA's back.
        self._beliefs_tab: "Any | None" = None
        # v0.48.0: whether this session's lore_core can record a belief
        # outcome or retract, fetched once when the beliefs picker first
        # opens (see PaneChipsMixin._prime_belief_action_state). None until
        # then, which renders as no caveat rather than a guessed one.
        self._belief_actions_state: "dict | None" = None
        # The second status row -- mounted the moment _subagents stops
        # being empty, unmounted the moment it is empty again (see
        # _sync_subagent_line); None at every other time, deliberately, so
        # an idle pane carries neither the widget nor its layout cost.
        self._subagent_line: "SubagentLine | None" = None
        # Status-chips (item Y): one provider seam instance per pane,
        # cached for the pane's whole life -- list_models() itself caches
        # its result too (see doxa.providers), but this is what makes THAT
        # cache actually persist across picker opens instead of being
        # rebuilt (and re-probing the network) every time.
        self._model_provider = providers_mod.ClaudeProvider()

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="block-list")
        yield StatusBar(self)
        # All four popups sit directly ABOVE the prompt (the last
        # children): in a terminal the block list simply gives up the rows
        # while one is open, which reads as an overlay without the layer
        # bookkeeping a floating panel would need over a TabbedContent. The
        # two ordinary ones are never open at once -- the slash dropdown
        # closes at the first space, which is exactly the keystroke that
        # opens the search popup. The needs-input dialog (queue item 5) is
        # independent of both -- see PromptInput.on_key's priority order.
        # The chip picker (status-chips, item Y) is independent of all
        # three too -- it opens from a status-bar click, never from
        # anything typed in the prompt.
        search = SessionSearch(self.cwd)
        yield search
        dropdown = SlashComplete()
        yield dropdown
        needs_input = NeedsInputPopup()
        yield needs_input
        yield ChipPicker(self)
        # No ``placeholder=`` -- TextArea has no built-in placeholder text
        # (Input did); a deliberate drop, not an oversight, see item N.
        yield PromptInput(dropdown, search, needs_input, id="prompt-input")

    async def on_mount(self) -> None:
        self.engine = self._engine_factory()
        if self._initial_pinned_name:
            # Item D restore: pin the saved name BEFORE the boot worker
            # starts, same as a user's own /rename -- set_custom_name is
            # the one place that writes a pinned label onto the tab
            # header, and it only needs the DOM (already mounted here),
            # never the engine.
            self.set_custom_name(self._initial_pinned_name)
        self.run_worker(self._boot(), exclusive=True, group="engine")
        self.run_worker(self._peer_pump(), exclusive=True, group="peers")

    # -- lifecycle ---------------------------------------------------

    async def detach(self) -> None:
        """Close-detach this pane's engine handle: over a daemon client
        finalize() only detaches (the daemon lingers); in-process it
        finalizes for real. Never raises -- teardown paths call this."""
        engine, self.engine = self.engine, None
        if engine is not None:
            with contextlib.suppress(Exception):
                await engine.finalize()

    async def _restore_transcript(self, session_id: str, cwd: str) -> None:
        """Put this reattached session's PRIOR CONVERSATION back on screen.

        The defect this closes, measured against a real daemon over a real
        socket before anything was changed: v0.23.0's restore reattaches
        and lets the daemon replay its event ring, which works only while
        the whole session still fits in 512 frames. One ``text_delta`` is
        one frame, so a single 700-delta answer pushed ``turn_started``
        off the ring; ``_peer_pump`` then had no TurnBlock to render the
        surviving deltas into and dropped every one -- the restored tab
        came up EMPTY, beside a live daemon that held the entire
        conversation, and said nothing about it.

        So the content comes from the session's persisted transcript
        instead (doxa.transcript): complete, already scrubbed, written at
        the engine's own persistence choke point, and on the same machine
        as this TUI. Nothing crosses the socket, so the 64KB frame cap
        that forced v0.28.0 to page the beliefs RPC is not on this path at
        all; what IS capped is the render (turn count and per-turn text,
        both reported on screen when they bite -- see mount_transcript).

        The live tail still comes from the daemon: this pane's client
        attached with ``skip_backlog``, so the ring is NOT replayed on top
        of what we just drew and no turn is rendered twice. A daemon too
        old to advertise its ring head refuses that skip
        (``backlog_skipped is None``), and then this method does nothing
        at all -- v0.31.0's replay-only behavior, unchanged, rather than a
        doubled transcript.

        Never fatal: an unreadable transcript costs the scrollback, never
        the session -- the pane is attached and usable either way."""
        engine = self.engine
        if getattr(engine, "backlog_skipped", None) is None:
            return
        block_list = self.query_one("#block-list", VerticalScroll)
        try:
            snapshot = await asyncio.to_thread(
                transcript_mod.read, session_id, cwd,
            )
        except Exception:  # noqa: BLE001 -- scrollback is never worth a crash
            return
        if not snapshot:
            return
        await mount_transcript(block_list, snapshot)

    def _refresh_identity(self) -> None:
        """Re-render the identity block in place -- after an auth flow the
        account, the plan tier and the organization may all have changed,
        and a stale identity block is worse than none."""
        try:
            block = self.query_one("#identity-block", SystemBlock)
        except Exception:
            return
        engine = self.engine
        cwd = str(getattr(engine, "cwd", None) or self.cwd)
        block.text = self._identity_text(cwd)
        block.update(f"▎ doxa\n{block.text}")

    # -- the tab's own label -----------------------------------------

    def auto_label(self) -> str:
        """`Opus@doxa:main` -- which model is answering, and WHAT IT IS
        WORKING OFF.

        The branch half is GitLine.tab_branch(): the worktree-per-session
        BASE (`main`) inside an isolated session, not the session's own
        throwaway branch (`doxa/f13526d4`, branch_label()'s answer, kept
        for the status bar/`/about` -- see that method's docstring for the
        v0.17 regression this un-does). Both halves are tracked state
        already: the model is the engine's (so a live /model switch moves
        it), and the repo/branch come from the pane's GitLine, whose reads
        are event-driven stats -- this adds no polling and no subprocess.
        OUTSIDE a repo there is nothing after the `@` that would mean
        anything, so the session names itself from its first turn
        (doxa/naming.py) and the directory name stands in until it does."""
        engine = self.engine
        model = short_model(getattr(engine, "model", None) or self.model)
        cwd = str(getattr(engine, "cwd", None) or self.cwd)
        git = self._git
        if git is not None and git.repo:
            branch, isolated = git.tab_branch()
            return compose_tab_label(model, git.repo, branch, isolated=isolated)
        return compose_tab_label(
            model, self.generated_name or Path(cwd).name or cwd
        )

    def display_name(self) -> str:
        """What this tab currently says -- the user's name for it if it has
        one, the automatic label otherwise."""
        return self.custom_name or self._tab_label or self.auto_label()

    def set_custom_name(self, name: "str | None") -> None:
        """Name this tab (pinning it), or pass None/"" to un-pin it and
        hand the label back to :meth:`auto_label`."""
        name = (name or "").strip()
        self.custom_name = name or None
        if self.custom_name:
            self.set_tab_label(ellipsize(self.custom_name))
        else:
            self._tab_label = None
            self.refresh_tab_label()
        # Item D: the pinned name is part of the persisted tab set --
        # rename it, restore it named. Safe before boot (this pane may not
        # have a session_id yet -- on_mount applies a restored pinned name
        # before the boot worker starts) since DoxaApp._persist_tabset
        # skips any pane whose session_id isn't known yet.
        # Deferred: doxa.app imports this package, so the arrow only
        # points back at call time -- the pane never needs the app
        # class before there is an app.
        from ..app import DoxaApp

        app = self.app
        if isinstance(app, DoxaApp):
            app._persist_tabset()

    def _maybe_name_tab(self, first_message: str) -> None:
        """After the FIRST completed turn of a repo-less session: ask Haiku
        for a name, once. Never on a tab the user named, never inside a
        repo (repo and branch already say where you are), and never twice
        -- a namer that failed must not retry in a loop."""
        if self._naming_done or self.custom_name:
            return
        git = self._git
        if git is not None and git.repo:
            return
        self._naming_done = True
        session_id = str(getattr(self.engine, "session_id", "") or "")
        self.run_worker(
            self._name_tab(session_id, first_message), group="naming"
        )

    async def _name_tab(self, session_id: str, first_message: str) -> None:
        name = await asyncio.to_thread(
            naming_mod.name_for, session_id, first_message
        )
        if not name or self.custom_name:
            return  # keep the dirname; the failure is final for this session
        self.generated_name = name
        self._tab_label = None
        self.refresh_tab_label()

    def refresh_tab_label(self) -> None:
        """Re-render the tab's label if it changed. Cheap, idempotent, and
        called from exactly where the status bar is refreshed. A NAMED tab
        keeps its name through every model switch and branch change --
        that is what pinning means."""
        if self.custom_name:
            return
        label = self.auto_label()
        if label == self._tab_label:
            return
        self.set_tab_label(label)

    def set_tab_label(self, text: str) -> None:
        """Write one label onto the tab header AND onto the pane's own
        title, which is what the palette's tab section and any later
        re-add of the pane read.

        `text` is the tab's plain IDENTITY -- what the rename field seeds
        from, what a later call compares against to skip a no-op render,
        what the user typed if they pinned the tab. The provider glyph is
        a display-only prefix layered on top HERE, never folded into that
        identity string: a pinned (user-renamed) tab still gets the glyph
        -- provider identity is orthogonal to the user's name for the tab
        -- but renaming it back to itself must not hand back
        "✳ my old name" as the seed."""
        self._tab_label = text
        displayed = f"{provider_glyph()} {text}"
        self._title = self.render_str(displayed)
        with contextlib.suppress(Exception):
            tabbed = self.app.query_one("#session-tabs", TabbedContent)
            tabbed.get_tab(self.id or "").label = displayed

    def _set_tab_class(self, class_name: str, value: bool) -> None:
        """Toggle one status class (``-working`` / ``-done-unseen`` /
        ``-attention``) on this pane's own Tab header -- same
        contextlib.suppress discipline as :meth:`set_tab_label`, and for
        the same reasons: the tab may not exist yet this early in boot, or
        this pane may already be mid-teardown (a closed tab's last event
        landing after the Tab widget is gone). Delegates to the module-level
        ``_write_tab_class``, the same door ``SubagentTranscriptTab`` uses
        for its own (``-done-unseen``-only) status class."""
        _write_tab_class(self.app, self.id or "", class_name, value)

    def set_needs_input(self, value: bool) -> None:
        """The attention-blink mechanism. Nothing calls this with True yet
        -- see the ``needs_input`` note in ``__init__`` -- but the timer
        discipline is real: a ``set_interval`` lives on this pane ONLY
        between a True call and the next False (or tab activation, which
        also clears it), never longer. That is what keeps an idle DOXA at
        zero timers even after this feature is wired up in phase 2."""
        if value == self.needs_input:
            return
        self.needs_input = value
        if value:
            self._attention_on = False
            self._attention_timer = self.set_interval(0.5, self._blink_attention)
        else:
            if self._attention_timer is not None:
                self._attention_timer.stop()
                self._attention_timer = None
            self._attention_on = False
            self._set_tab_class("-attention", False)

    def _blink_attention(self) -> None:
        self._attention_on = not self._attention_on
        self._set_tab_class("-attention", self._attention_on)

    def set_staged(self, value: bool) -> None:
        """"This tab has staged memory proposals you have not looked at" --
        the ``-staged`` tab class, written through the SAME
        :meth:`_set_tab_class` door as ``-working``/``-done-unseen``/
        ``-attention``. Not a second attention mechanism: one tab-status
        vocabulary, one write path, one precedence ladder in theme.tcss.

        A STEADY TINT, and deliberately not a blink. Blinking is reserved
        for ``-attention`` (needs-input), and the reservation is not
        stylistic: a blink says "the session is stopped until you act",
        which is true of a permission prompt and false of a staged
        proposal -- nothing is blocked, nothing expires, and nothing
        reaches curated memory until a human approves it in LORE. A signal
        that shouted would be lying about the stakes, and a second blinking
        thing on the tab strip would make the one that IS urgent stop
        reading as urgent. It also costs no timer: the blink needs a
        ``set_interval`` alive for its whole duration, and this state can
        legitimately persist for a whole session, which is exactly the
        busy-idle bug the ``needs_input`` note in ``__init__`` warns about.

        Cleared when you activate the tab (you have now seen the notice --
        the block itself is in the transcript to come back to) or when you
        open the list, the same "looking at it counts" rule
        ``-done-unseen`` follows."""
        if value == self.staged_pending:
            return
        self.staged_pending = value
        self._set_tab_class("-staged", value)

    @on(TextArea.Changed, "#prompt-input")
    def _on_prompt_changed(self, event: TextArea.Changed) -> None:
        """The two popups' only trigger: what the prompt currently says.
        Cheap by construction -- a registry scan of a handful of rows for
        the dropdown, and for the search popup a debounce timer rather than
        a query (this app does not poll, and it does not hit SQLite on a
        keystroke either). PromptInput's OWN ``on_text_area_changed``
        (box-height resize) has already run by the time this bubbles here
        -- it deliberately does not stop the event."""
        event.stop()
        if self.query_one("#needs-input-popup", NeedsInputPopup).is_open:
            # A pending question owns this row while it is up -- typing
            # still works (composing a note is fine), but the two ordinary
            # popups must not pop up underneath/instead of it.
            return
        text = event.text_area.text
        self.query_one("#slash-complete", SlashComplete).sync(text)
        self.query_one("#session-search", SessionSearch).sync(text)

    @on(OptionList.OptionSelected, "#slash-complete")
    def _on_slash_selected(self, event: OptionList.OptionSelected) -> None:
        """Clicking an entry completes it, same as Tab/Enter would."""
        event.stop()
        prompt = self.query_one("#prompt-input", PromptInput)
        prompt.dropdown.highlighted = event.option_index
        prompt.complete()
        prompt.focus()

    @on(OptionList.OptionSelected, "#chip-picker")
    def _on_chip_picker_selected(self, event: OptionList.OptionSelected) -> None:
        """Enter (OptionList's own ``action_select``) and a mouse click on
        a row (OptionList's own ``_on_click``) both post this SAME
        message -- one handler covers keyboard and mouse selection."""
        event.stop()
        self.query_one("#chip-picker", ChipPicker).select_row(event.option_index)

    @on(events.Click)
    def _on_click_away_closes_chip_picker(self, event: events.Click) -> None:
        """A click ANYWHERE in this pane other than the picker itself
        closes it -- clicking one of the status-bar's own `[@click=...]`
        spans never reaches here (``Widget.broker_event`` calls
        ``event.stop()`` the moment it resolves an action, before the
        event would bubble up to this pane-level handler), and a click on
        the picker's own rows is handled by ``_on_chip_picker_selected``
        above (OptionList's ``_on_click`` does not itself stop the event,
        so it still bubbles here too -- the ``event.widget is picker``
        check below is what keeps that harmless). Focus genuinely moving
        elsewhere (a tab switch, clicking another focusable widget) is
        handled separately by ChipPicker's own ``_on_blur``, since that
        case never fires a Click that bubbles through this pane at all."""
        picker = self.query_one("#chip-picker", ChipPicker)
        if picker.is_open and event.widget is not picker:
            picker.close()

    @on(OptionList.OptionSelected, "#needs-input-popup")
    def _on_needs_input_selected(self, event: OptionList.OptionSelected) -> None:
        """Clicking a row answers it, same as a number key or Enter would.
        ``event.option_index`` is offset by the disabled heading row at 0
        -- see :class:`NeedsInputPopup`'s own row convention."""
        event.stop()
        popup = self.query_one("#needs-input-popup", NeedsInputPopup)
        index = event.option_index - 1
        if index < 0:
            return
        self.run_worker(
            self._resolve_needs_input(popup, index, False),
            exclusive=True, group="needs-input",
        )

    @on(PromptInput.NeedsInputChoice)
    def _on_needs_input_key_choice(self, event: "PromptInput.NeedsInputChoice") -> None:
        event.stop()
        self.run_worker(
            self._resolve_needs_input(event.popup, event.index, False),
            exclusive=True, group="needs-input",
        )

    @on(PromptInput.NeedsInputDecline)
    def _on_needs_input_key_decline(self, event: "PromptInput.NeedsInputDecline") -> None:
        event.stop()
        self.run_worker(
            self._resolve_needs_input(event.popup, None, True),
            exclusive=True, group="needs-input",
        )

    @on(OptionList.OptionSelected, "#session-search")
    def _on_search_selected(self, event: OptionList.OptionSelected) -> None:
        """Clicking a row does what Enter would: toggle a session header,
        or take a snippet's excerpt."""
        event.stop()
        prompt = self.query_one("#prompt-input", PromptInput)
        prompt.search.highlighted = event.option_index
        if prompt.search.current_kind() == "header":
            prompt.search.toggle_current()
        else:
            prompt.search.take_hit()
        prompt.focus()

    @on(PromptInput.Submitted)
    def on_prompt_submitted(self, event: "PromptInput.Submitted") -> None:
        event.stop()  # this pane's prompt is nobody else's business
        self.query_one("#slash-complete", SlashComplete).close()
        self.query_one("#session-search", SessionSearch).close()
        prompt = event.value.strip()
        if not prompt:
            return
        event.control.clear()
        # `!` (item Q): the ONE place a shell command can be dispatched
        # from, and it is reached only from PromptInput.Submitted, which
        # the prompt posts only from its own submit key binding. Checked
        # before the slash registry because `!` is deliberately NOT a
        # registry row -- see doxa.shell's module docstring for why the
        # executor must stay off every surface that dispatches by name.
        if prompt.startswith(SHELL_PREFIX):
            self.run_worker(
                self._run_shell(prompt[len(SHELL_PREFIX):]), group="shell"
            )
            return
        # Only rows of the slash registry (doxa/commands.py) are
        # intercepted, and passthrough rows deliberately are not: the
        # literal "/compact" convention has to REACH the CLI to do anything.
        command = commands_mod.lookup(prompt)
        if command is not None and not command.passthrough:
            self.run_worker(self._run_command(prompt), group="command")
            return
        self.run_worker(self._run_turn(prompt), exclusive=True, group="turn")

    @on(PromptInput.ClipboardImageNotice)
    async def on_clipboard_image_notice(
        self, event: "PromptInput.ClipboardImageNotice"
    ) -> None:
        event.stop()
        await self._system(
            f"clipboard holds an image ({event.mime}) — image attachments "
            "aren't wired into turns yet; save it to a file and use "
            "/img <path>, or paste it somewhere that turns it into a "
            "file DOXA can point at"
        )

    # -- the three doors a submitted line goes through ----------------

    async def _system(self, text: str) -> None:
        """Mount one doxa-generated block and stay scrolled to it."""
        block_list = self.query_one("#block-list", VerticalScroll)
        await block_list.mount(SystemBlock(text))
        block_list.scroll_end(animate=False)

    async def _run_command(self, prompt: str) -> None:
        await self._engine_ready.wait()
        name, _, args = prompt.strip().partition(" ")
        handler = self._command_handlers().get(name)
        if handler is None:  # registry/handler drift -- the closure test's job
            await self._system(f"unknown command: {name}")
            return
        await handler(args.strip())

    async def _run_shell(self, command: str) -> None:
        """Item Q's executor side. **Read doxa/shell.py's module docstring
        before calling this from anywhere new** -- it runs an arbitrary
        command with the user's full privileges, and it is safe only
        because its single caller is a keystroke.

        Deliberately NOT ``_run_command``: this method is not in the
        handler dict, ``!`` is not in the slash registry, and there is
        therefore no name a dispatcher (a status-chip click, a peer
        message, a future plugin row) could pass to arrive here.

        The command runs in the SESSION's directory -- its own worktree
        when worktree-per-session is on -- which is why the engine is
        awaited first: ``!git status`` must report on the tree the model is
        editing, not on wherever DOXA was launched from. The block is
        mounted before the process is even started, and updated in place
        when it ends, so a slow command is visibly running rather than
        looking like a swallowed keystroke; the Textual worker this runs
        under is what keeps the prompt and the session live meanwhile.

        Neither the command nor its output is sent to the model or
        persisted to the session transcript."""
        command = command.strip()
        if not command:
            await self._system(
                "shell: `!<command>` runs a command in this session's "
                "directory and shows its output here. It never reaches the "
                "model — not the command, not the output."
            )
            return
        await self._engine_ready.wait()
        cwd = str(getattr(self.engine, "cwd", None) or self.cwd)
        block_list = self.query_one("#block-list", VerticalScroll)
        block = ShellBlock(command, cwd)
        await block_list.mount(block)
        block_list.scroll_end(animate=False)
        result = await shell_mod.run(command, cwd)
        block.complete(result)
        block_list.scroll_end(animate=False)

# SPDX-License-Identifier: AGPL-3.0-only
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
from textual.containers import Vertical, VerticalScroll
from textual.widgets import OptionList, TabbedContent, TextArea

from .. import commands as commands_mod
from .. import layout as layout_mod
from .. import history as history_mod
from .. import naming as naming_mod
from .. import shell as shell_mod
from .. import providers as providers_mod
from .. import transcript as transcript_mod
from ..history import SessionSearch
from ..shell import SHELL_PREFIX
from ..ui.dialogs import (
    ChipPicker,
    NeedsInputPopup,
    ResumeConfirm,
    SlashComplete,
)
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

#: Serial behind :func:`_auto_pane_id`. A leaf needs an id of its own now
#: that it is no longer the tab (v0.91.0): the peers chip jumps to a pane
#: BY id (``DoxaApp._switch_to_tab``), directional focus keys the painted
#: rectangles by id (``DoxaApp._pane_regions``), and both used to get one
#: for free from the ``TabPane`` this class no longer is. Process-wide and
#: monotonic, so two panes are never confusable even across tabs.
_PANE_SERIAL = 0


def _auto_pane_id() -> str:
    global _PANE_SERIAL
    _PANE_SERIAL += 1
    return f"pane-{_PANE_SERIAL}"


class SessionPane(PaneCommandsMixin, PaneChipsMixin, PaneRuntimeMixin, Vertical):
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
    sees them, and every method's behaviour are unchanged.

    **v0.91.0 changed the base class, and only the base class.** This was
    a ``TabPane`` from Phase 3 until then -- one tab, one session, and
    "which pane is active" derivable from "which tab is showing". Splits
    break that equivalence: two of these can be visible in ONE tab, and a
    ``TabPane`` inside a ``TabPane`` is a widget whose ``Focused`` message
    reassigns ``TabbedContent.active`` to an id that is not a tab. So the
    TAB became a container of its own (:class:`doxa.ui.split.PaneTab`) and
    this became an ordinary ``Vertical``. Everything a caller touches is
    unchanged, including ``self.query_one("#block-list", ...)`` and the
    rest of the pane-scoped queries -- this widget's subtree is exactly
    what it always was, it simply no longer IS the tab that holds it.
    :attr:`tab` and :attr:`tab_id` are how the label/status-class writers
    reach the tab that does."""

    def __init__(
        self,
        title: str,
        cwd: str,
        model: str | None,
        engine_factory: "Callable[[], Any]",
        *,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id or _auto_pane_id())
        #: The label this pane is BORN with -- handed to the PaneTab that
        #: holds it (a leaf has no tab header of its own), and kept here
        #: so a pane built before its tab still knows what to call itself.
        self.born_title = title
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
        # Set True at the top of stop(): this engine's daemon was told to
        # finalize for real, as opposed to detach()'s "handle cleared, but
        # nobody told the daemon to stop". Through v0.55.0 this also
        # excluded the pane from the persisted tab set entirely -- v0.60.0
        # dropped that (see DoxaApp._ended_this_run's docstring): a
        # finalized session is a resumable one now, so nothing branches on
        # this flag any more.
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
        # v0.56.0 (/resume): this pane's engine CONTINUES an existing
        # conversation. Set at construction by DoxaApp.resume_session, read
        # once by _boot, which then reuses v0.32.0's own
        # _restore_transcript to put the prior turns back on screen -- see
        # that method's `require_backlog_skip` argument for the one thing
        # a resume does differently from a reattach.
        self._resume_from: "str | None" = None
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
        # opening block's `lore` line (v0.56.0 -- see _lore_memory_bits).
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
        # v0.48.0: whether this session's lore_core can record a belief
        # outcome or retract, fetched once when the beliefs picker first
        # opens (see PaneChipsMixin._prime_belief_action_state). None until
        # then, which renders as no caveat rather than a guessed one.
        self._belief_actions_state: "dict | None" = None
        # v0.57.0: whether this session may APPROVE a staged proposal --
        # lore_write_state, the wider gate (a new entry needs a `via`
        # label). Fetched once when the proposals picker first opens.
        self._pending_writes_state: "dict | None" = None
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
        # v0.91.0: this pane's OWN "you missed something" state, per class
        # name. Through v0.88.0 the tab header WAS this state -- one tab,
        # one pane, so writing the class was recording it. A tab can now
        # hold several panes, and the header can only carry one answer for
        # all of them, so the answer moved here and the header became the
        # OR over a tab's leaves (see _set_tab_class). Read by the tests
        # that pin "visible but unfocused is not seen".
        self._marks: "dict[str, bool]" = {}
        # The in-pane divider's position: the fraction of this pane's
        # height the PROMPT AREA holds. 0.0 means "nobody has moved it",
        # which is the content-driven auto height DOXA has always had --
        # an explicit zero and an unset divider are the same statement, so
        # a record that carries no ratio restores to today's behaviour
        # exactly. Proportional, never rows: see doxa.layout.
        self.prompt_ratio: float = 0.0

    # -- the tab that holds this pane (v0.91.0) -----------------------

    @property
    def tab(self) -> "Any | None":
        """The :class:`doxa.ui.split.PaneTab` this pane is a leaf of, or
        ``None`` before it is mounted. Walked rather than stored: a pane
        is mounted into its tab, never told about it, and a stored
        reference would be one more thing to keep true."""
        from ..ui.split import PaneTab

        node: Any = self.parent
        while node is not None and not isinstance(node, PaneTab):
            node = node.parent
        return node

    @property
    def tab_id(self) -> str:
        """The id of the TAB this pane lives in -- what the tab strip, the
        rename field and the status classes key off. ``""`` before mount,
        which every caller already treats as "no tab to write to"."""
        tab = self.tab
        return (tab.id or "") if tab is not None else ""

    @property
    def is_split_leaf(self) -> bool:
        """Is this pane sharing its tab with another pane?"""
        tab = self.tab
        return tab is not None and len(tab.leaves()) > 1

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
        picker = ChipPicker(self)
        yield picker
        # No ``placeholder=`` -- TextArea has no built-in placeholder text
        # (Input did); a deliberate drop, not an oversight, see item N.
        yield PromptInput(dropdown, search, needs_input, picker, id="prompt-input")

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

    async def _restore_transcript(
        self, session_id: str, cwd: str, *, require_backlog_skip: bool = True,
    ) -> None:
        """Put this session's PRIOR CONVERSATION back on screen.

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

        ``require_backlog_skip=False`` is the RESUME caller (v0.56.0), and
        it is the only difference between the two. A reattach shares a
        daemon that has been running all along, so drawing from disk is
        only safe once that daemon has agreed not to replay its ring on
        top -- hence the guard. A resume has no such daemon: its process
        was started seconds ago for exactly this conversation and has
        nothing buffered to replay, so the guard would refuse the one case
        where drawing from disk is unconditionally correct. Same reader,
        same renderer, same file -- one precondition that only one of the
        two callers has.

        Never fatal: an unreadable transcript costs the scrollback, never
        the session -- the pane is usable either way."""
        engine = self.engine
        if require_backlog_skip and getattr(engine, "backlog_skipped", None) is None:
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
        # A SPLIT leaf does not own the tab header -- the tab's FIRST leaf
        # names it, and a second session sharing the tab is found by
        # looking at it, not by reading a label that could only ever say
        # one of the two names. (The palette's tab section still lists
        # every session by id, which is where two panes in one tab are
        # actually told apart.) The pane's own ``_tab_label`` above is
        # still written either way, so ``display_name`` -- which the
        # rename field, the palette and the detach toast all read -- keeps
        # naming THIS session rather than its tab's.
        tab = self.tab
        if tab is not None:
            leaves = tab.leaves()
            if leaves and leaves[0] is not self:
                return
            tab._title = self.render_str(displayed)
        with contextlib.suppress(Exception):
            tabbed = self.app.query_one("#session-tabs", TabbedContent)
            tabbed.get_tab(self.tab_id).label = displayed

    def _set_tab_class(self, class_name: str, value: bool) -> None:
        """Toggle one status class (``-working`` / ``-done-unseen`` /
        ``-attention`` / ``-staged``) for this pane.

        v0.91.0 made this write TWO places, because a tab can now hold
        more than one pane and the header can carry only one answer:

        * on the PANE itself, as a CSS class, so a split leaf shows its
          own state where the user can see which pane it is about --
          ``SessionPane.-done-unseen`` and friends in ``doxa/theme.tcss``;
        * on the tab header, as the OR over the tab's leaves, so a tab
          whose corner pane finished still reads as "something happened in
          here" from the strip.

        Same contextlib.suppress discipline as :meth:`set_tab_label`, and
        for the same reasons: the tab may not exist yet this early in
        boot, or this pane may already be mid-teardown (a closed tab's
        last event landing after the Tab widget is gone)."""
        self._marks[class_name] = value
        with contextlib.suppress(Exception):
            self.set_class(value, class_name)
        tab = self.tab
        if tab is None:
            _write_tab_class(self.app, "", class_name, value)
            return
        any_on = any(
            leaf._marks.get(class_name, False) for leaf in tab.leaves()
        )
        _write_tab_class(self.app, tab.id or "", class_name, any_on)

    def has_mark(self, class_name: str) -> bool:
        """Does THIS pane still carry that "you missed something" mark?

        The question the spec settles: a pane that is merely VISIBLE has
        not been seen. Activation clears the marks of the pane that got
        the keyboard, never of its siblings -- see
        ``DoxaApp._clear_seen_marks``."""
        return bool(self._marks.get(class_name, False))

    # -- the in-pane divider: the status bar (v0.91.0) ----------------

    def nudge_prompt(self, rows: int) -> bool:
        """Move the status-bar divider by ``rows`` -- positive grows the
        PROMPT area (Ctrl+Down), negative grows the transcript (Ctrl+Up).
        Returns whether it moved.

        The status bar IS the divider inside one pane: :meth:`compose`
        yields the scrolling ``#block-list``, then the ``StatusBar``, then
        the popups, then ``#prompt-input``, so the status line is
        literally the boundary between the transcript above and the prompt
        area below. This needs no notion of a "selected divider" and no
        focus rule of its own -- the handle is a fixed, always-present
        piece of furniture, present in a single-leaf tab with no splits at
        all, which is the case that provoked the whole request (reviewing
        166 staged proposals in a surface too short for them).

        Stored as a RATIO of the pane's height, so the position survives a
        terminal resize and a restore into a different window; converted
        to rows only at the moment of painting (:meth:`_apply_prompt_ratio`)."""
        height = max(1, self.content_size.height or self.size.height or 1)
        current = self._effective_prompt_rows()
        target = current + rows
        floor, ceiling = self._prompt_bounds(height)
        target = max(floor, min(ceiling, target))
        if target == current:
            return False
        self.prompt_ratio = layout_mod.clamp_prompt_ratio(
            (target + 2) / height  # +2: the prompt's round border
        )
        self._apply_prompt_ratio()
        return True

    def _prompt_bounds(self, height: int) -> "tuple[int, int]":
        """The prompt's content-row floor and ceiling for a pane of
        ``height`` rows. The floor is the point of the whole rule: a
        resize must never leave the input line too small to type into,
        which is the one region whose collapse makes DOXA unusable rather
        than merely awkward. The ceiling keeps a readable transcript above
        it, so the divider cannot swallow the conversation being typed
        into."""
        # -1 status bar, -2 the prompt's round border, -1 its bottom
        # margin (theme.tcss: `#prompt-input { margin: 0 1 1 1 }`) -- all
        # four are rows the pane spends before either region gets one, and
        # leaving the margin out is exactly how a "3-row floor" renders as
        # a 2-row transcript.
        ceiling = height - layout_mod.MIN_TRANSCRIPT_ROWS - 1 - 2 - 1
        return layout_mod.MIN_PROMPT_ROWS, max(layout_mod.MIN_PROMPT_ROWS, ceiling)

    def _effective_prompt_rows(self) -> int:
        """How many CONTENT rows the prompt is showing right now."""
        try:
            prompt = self.query_one("#prompt-input", PromptInput)
        except Exception:  # noqa: BLE001 -- not composed yet
            return PromptInput.MIN_ROWS
        pinned = getattr(prompt, "pinned_rows", None)
        if pinned:
            return int(pinned)
        return max(
            PromptInput.MIN_ROWS,
            min(PromptInput.MAX_ROWS, prompt.wrapped_document.height),
        )

    def _apply_prompt_ratio(self) -> None:
        """Turn the stored ratio into the prompt's pinned row count.

        Called on every resize as well as on every divider move, which is
        what makes the position PROPORTIONAL rather than a column count
        that happened to look right on one terminal."""
        try:
            prompt = self.query_one("#prompt-input", PromptInput)
        except Exception:  # noqa: BLE001 -- not composed yet
            return
        if not self.prompt_ratio:
            prompt.pinned_rows = None
            prompt.sync_height()
            return
        height = max(1, self.content_size.height or self.size.height or 1)
        floor, ceiling = self._prompt_bounds(height)
        rows = int(round(self.prompt_ratio * height)) - 2  # -2: the border
        prompt.pinned_rows = max(floor, min(ceiling, rows))
        prompt.sync_height()

    def on_resize(self, event: events.Resize) -> None:
        """A proportional divider is only proportional if something
        re-derives its rows when the pane changes size."""
        if self.prompt_ratio:
            self._apply_prompt_ratio()

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
        -- it deliberately does not stop the event.

        v0.67.0: a THIRD sync target, the beliefs/proposals ChipPicker --
        only while it is actually driving the prompt (``prompt_filter_
        active``; every other chip menu ignores this entirely, unchanged).
        Checked regardless of the needs-input guard below: PromptInput's
        own ``on_key`` already gives needs-input priority over the picker
        for KEYSTROKES, but a Changed event fires from whatever text is on
        screen NOW, and the picker's filter must track that text exactly
        the same way the other two popups' do."""
        event.stop()
        picker = self.query_one("#chip-picker", ChipPicker)
        if picker.prompt_filter_active:
            picker.sync_filter(event.text_area.text)
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
        """Clicking a row does what Enter would: offer to RESUME a session
        header (v0.56.0 -- it used to toggle that header's fold), or take a
        snippet's excerpt.

        The two must not drift. Enter's meaning on a header changed in
        ``PromptInput.on_key``, and a click that still toggled would leave
        one popup with two answers to "activate this row" -- so this reads
        the same :meth:`SessionSearch.chosen_session` and posts the same
        message. Expanding and collapsing keep Right and Left, which is
        what made Enter free to be repurposed at all."""
        event.stop()
        prompt = self.query_one("#prompt-input", PromptInput)
        prompt.search.highlighted = event.option_index
        group = prompt.search.chosen_session()
        if group is not None:
            prompt.post_message(PromptInput.ResumeRequested(group))
        else:
            prompt.search.take_hit()
        prompt.focus()

    @on(PromptInput.ResumeRequested)
    def _on_resume_requested(self, event: "PromptInput.ResumeRequested") -> None:
        """Enter (or a click) on a /search session header: confirm, then
        resume. A worker because both halves must be awaited -- a modal's
        answer (``push_screen_wait`` is legal only from one) and the tab
        the app then opens. Exclusive in its own group so a second Enter
        while the dialog is up cannot start a second resume."""
        event.stop()
        self.run_worker(
            self._confirm_and_resume(event.group), exclusive=True, group="resume",
        )

    async def _confirm_and_resume(self, group: dict) -> None:
        """Ask, then act -- or, when the conversation cannot be resumed at
        all, say so in the same dialog and act on nothing.

        Eligibility is decided BEFORE the dialog and off the loop
        (:func:`doxa.history.resume_state` reads the peer registry and two
        directories), so what the user sees already knows whether it is
        offering a resume or explaining a refusal. Asking first and
        failing after would move the discovery to one turn INTO a
        conversation the user believed they had reopened, which is
        precisely the failure that check exists to prevent.

        A session that is still RUNNING never reaches the dialog: there is
        nothing to confirm, because resuming is not the right verb for it
        -- see :meth:`DoxaApp.resume_session`, which owns that decision so
        this gesture and ``/resume`` cannot answer it differently."""
        from ..app import DoxaApp  # deferred: doxa.app imports this package

        session_id = str(group.get("session_id") or "")
        cwd = str(group.get("cwd") or "")
        state, reason = await asyncio.to_thread(
            history_mod.resume_state, session_id, cwd
        )
        app = self.app
        if not isinstance(app, DoxaApp):
            return
        if state != history_mod.RESUME_RUNNING:
            dialog = ResumeConfirm(
                str(group.get("title") or ""),
                session_id,
                when=history_mod.hit_age(group),
                cwd=cwd,
                reason=reason,
            )
            if not await app.push_screen_wait(dialog):
                return
        note = await app.resume_session(group)
        if note:
            await self._system(note)

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
        # THE GUARD (reported: "/lore:pending does nothing, no error"):
        # `lookup()` returning None does NOT mean this line is bogus --
        # /compact and every adopted plugin row (doxa.commands._plugin_rows)
        # answer None too, on purpose, and must still reach the CLI
        # untouched below. Only a "/"-prefixed line that answers to NOBODY
        # -- not a registry row, not a currently-adopted plugin command --
        # gets stopped here; commands_mod.unreachable_message returns None
        # for everything else, including every non-slash prompt. See its
        # docstring for what this can and cannot honestly claim.
        if prompt.startswith("/"):
            name = prompt.partition(" ")[0]
            message = commands_mod.unreachable_message(name)
            if message is not None:
                self.run_worker(self._run_unreachable(message), group="command")
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

    async def _run_unreachable(self, message: str) -> None:
        """The guard's own door: mount ``message`` (already built by
        ``commands_mod.unreachable_message``) and stop -- no engine wait,
        no worker on ``group="turn"``, because there is nothing to send.
        A plain method rather than inlining ``self._system(...)`` at the
        call site so the guard's dispatch in ``on_prompt_submitted`` reads
        the same shape as the other two doors (``_run_command``,
        ``_run_shell``, ``_run_turn``), each one line that names a
        coroutine."""
        await self._system(message)

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

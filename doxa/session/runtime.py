# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.session.runtime -- boot, the turn loop, the event dispatch, stop.

Everything a pane does that is driven by something other than a keystroke:
connecting the engine, running a turn, rendering the events that come back,
the peer pump that renders turns another attached client is driving, the
subagent tracker, and the two ways a session ends.

docs/plans/plugin-api.md's third extension point is :data:`EVENT_RENDERERS`. The
``if/elif`` chain over six event types that used to be
:meth:`PaneRuntimeMixin._handle_event`'s whole body is now a dispatch map
from event type to the method that renders it, one method per type. An
event type with no entry is ignored, exactly as it was before -- the old
chain had no ``else`` either. A plugin adding an event type would add a
row; the spec's rule that it may not REPLACE a built-in row (a plugin that
can silently redraw ``tool_result`` can lie to the user about what a tool
did) is the reason this map is a module constant and not pane state.
"""

from __future__ import annotations

import asyncio
import contextlib

from textual.css.query import NoMatches
from textual.containers import VerticalScroll
from textual.widgets import Static

from .. import banner as banner_mod
from .. import diff as diff_mod
from .. import identity as identity_mod
from .. import keyboard as keyboard_mod
from .. import naming as naming_mod
from .. import notify as notify_mod
from ..events import EngineEvent
from ..history import SessionSearch
from ..ui.dialogs import NeedsInputPopup
from ..ui.labels import (
    _escape_markup,
    _needs_input_summary,
    _subagent_label,
    unreachable_notice,
)
from ..ui.prompt import PromptInput
from ..ui.statusline import GitLine
from ..ui.transcript import (
    BootBanner,
    PeerMessageBlock,
    SubagentLine,
    SubagentTranscriptTab,
    SystemBlock,
    ToolChip,
    TurnBlock,
    _composed,
)


# Event type -> the method on the pane that renders it. See the module
# docstring: this is the dispatch map docs/plans/plugin-api.md's transcript-block
# extension point attaches to, and it is a module constant so a built-in
# row cannot be swapped out per pane.
EVENT_RENDERERS: "dict[str, str]" = {
    "turn_started": "_render_turn_started",
    "text_delta": "_render_text_delta",
    "reasoning_delta": "_render_reasoning_delta",
    "tool_call": "_render_tool_call",
    "tool_result": "_render_tool_result",
    "turn_done": "_render_turn_done",
}


class PaneRuntimeMixin:
    """SessionPane's engine-driven half. Mixed into the pane, never used
    standalone: every method here reads pane state through ``self``."""

    async def stop(self) -> "str | None":
        """Finalize this pane's session NOW (daemon included). Returns the
        worktree-per-session (#3) closing note -- `kept doxa/<id> — merge
        when ready` -- when the daemon kept an unfinished worktree instead
        of removing it; None otherwise (in-process engines never have
        one; a cleanly-removed or non-worktree session doesn't either)."""
        # Item D: marked BEFORE the engine handle is even cleared. Through
        # v0.55.0 this flag (not "engine is None", which detach() also
        # produces) was what told _persist_tabset to drop the pane from
        # the persisted tab set outright -- ending a session meant losing
        # the tab for good. v0.60.0 dropped that read for one release (see
        # DoxaApp._ended_this_run's docstring): a finalized session's
        # transcript is genuinely resumable via --resume, and v0.60.0 read
        # that as reason enough to keep the tab's record around too. It
        # is not the same fact -- Ctrl+Q is the user asking to be done
        # with this tab, not asking for it back next launch -- so v0.99.1
        # has _persist_tabset read this flag again (its own mounted-pane
        # scan), the same job it did through v0.55.0: keep this session
        # out of the tab set the NEXT launch restores, whether or not it
        # is also the reason CLI's own --resume can still replay it if
        # asked for by name.
        self._stopped = True
        engine, self.engine = self.engine, None
        if engine is None:
            return None
        stop = getattr(engine, "stop", None)
        note: "str | None" = None
        with contextlib.suppress(Exception):
            if stop is not None:
                event = await stop()
                data = getattr(event, "data", None) or {}
                value = data.get("note") if isinstance(data, dict) else None
                note = str(value) if value else None
            else:
                await engine.finalize()
        return note

    async def _build_and_boot(self) -> None:
        """Build this pane's engine OFF the loop, then boot it.

        **This is the fix for "after splitting the pane with vsplit, the
        whole TUI lags hard".** ``SessionPane.on_mount`` used to open with
        a bare ``self.engine = self._engine_factory()``. In the suite that
        factory returns a ``FakeEngine`` instantly and nothing showed; in
        production it is ``doxa.cli.new_session_factory`` ->
        ``daemon.spawn_daemon``, which ``subprocess.Popen``s a fresh
        daemon and then blocks its caller in a
        ``while monotonic() < deadline: _time.sleep(0.1)`` registry poll
        for up to ``wait_secs`` -- 60 seconds as written.

        Measured with a 10ms heartbeat task watching the loop across
        ``split_active_pane(ROW)``: idle inter-tick gap 12.0 ms, gap
        DURING the split 2320.8 ms, gap after 12.7 ms -- one unbroken
        ~2.3-second freeze, about 190x idle, with only four heartbeat
        ticks landing in the whole window. No keys, no repaint, no message
        pump. The reporter's own guess ("not async?") was exactly right,
        and their other guess (a CPU loop that never yields) was not: idle
        cost was measured at 0.8-2.0% of one core before AND after a
        split, across eleven scenarios and again through a real PTY, and
        never diverged. The app is not busy after a split; it was frozen
        during it.

        ``/split`` and ``/vsplit`` are where it is most visible because
        splitting is the gesture that mounts a pane while you are looking
        at another one that stops repainting. ``Ctrl+T`` (``action_new_tab``)
        and every restored tab went through the same line and had the same
        freeze.

        The discipline was already here, one method away: ``switch_engine``
        below has built its engine with ``await asyncio.to_thread`` since
        it was written, and its docstring gives this exact reason --
        "off-loop -- a daemon spawn blocks on subprocess+registry
        polling". ``/model`` and ``/attach`` were paying attention to it;
        the path EVERY pane takes was not.

        Failure surfaces as a block in this pane rather than an escaping
        exception, same as ``switch_engine``: a session that could not
        spawn is a thing to read, not a crash. And a pane that was closed
        while the thread was still working gets its engine finalized
        instead of stranded -- ``detach``/``stop`` cannot clear a handle
        that does not exist yet, so this is the one place that can."""
        try:
            engine = await asyncio.to_thread(self._engine_factory)
        except Exception as exc:  # noqa: BLE001 -- a spawn failure is a block, not a crash
            # ``_system``, not a raw mount: it holds the NoMatches AND
            # is_mounted pair this exact window needs (its own docstring
            # explains why one guard is not enough), and this runs from a
            # worker on a pane that may still be composing.
            await self._system(f"session failed to start: {exc}")
            return
        if self._stopped or not self.is_mounted:
            # Closed mid-spawn. Nothing will ever read this handle, and
            # dropping it on the floor leaks a daemon that thinks it has
            # a client.
            with contextlib.suppress(Exception):
                await engine.finalize()
            return
        self.engine = engine
        await self._boot()

    async def _boot(self) -> None:
        assert self.engine is not None
        if not self._adopted:
            # An ADOPTED handle (v0.97.0: this pane inherited a live
            # session from a pane in another group) is already started, and
            # every engine kind reads start() as "begin" -- see
            # SessionPane.adopt_engine for the two ways that goes wrong.
            await self.engine.start()
        # Engine cwd wins over the pane's own (attach may cross projects);
        # GitLine's constructor runs one git subprocess -- off the loop.
        git_cwd = str(getattr(self.engine, "cwd", None) or self.cwd)
        self._git = await asyncio.to_thread(GitLine, git_cwd)
        # /search scopes "this project first" by cwd, and attach can land
        # this pane in another project: the engine's cwd wins here too.
        with contextlib.suppress(Exception):
            self.query_one("#session-search", SessionSearch).cwd = git_cwd
        # A session named on an earlier run keeps that name across restarts
        # -- the cache IS the persistence, and reusing it is what stops a
        # restore from re-spending a call per restored tab.
        session_id = str(getattr(self.engine, "session_id", "") or "")
        # Item D: cache the id where detach()/stop() clearing self.engine
        # cannot take it with them -- see the attribute's own docstring.
        # Set on EVERY boot, not just the first: switch_engine (an attach
        # or a fresh /model session landing in this same tab) re-runs
        # _boot, and the persisted record has to follow which session this
        # tab actually holds now, not the one it opened with.
        self._session_id = session_id
        if session_id and not self.generated_name:
            cached = await asyncio.to_thread(naming_mod.cached_name, session_id)
            if cached:
                self.generated_name = cached
                self._naming_done = True
        self._refresh_usage_chip()
        self._engine_ready.set()
        self._refresh_status()
        # Initial identity block: who/where this session actually is --
        # only fields the CLI/config really reported, never guesses.
        #
        # The block list may not be mounted yet. _boot runs as a worker and
        # a restore mounts several panes at once, so this can reach a pane
        # Textual has not composed. query_one raises NoMatches there, from a
        # background task with no caller of ours to catch it, and the error
        # surface turns it into a visible block -- a failure report for a
        # frame nobody could have painted. Same shape and same reason as the
        # #status-bar guard in doxa.session.chips (v0.70.0); measured under
        # test_restore_view's saved-active-tab case, which mounts three.
        try:
            block_list = self.query_one("#block-list", VerticalScroll)
        except NoMatches:
            return
        # The banner introduces the identity block, so it mounts first and
        # only where there was nothing before it -- switch_engine re-runs
        # _boot into a pane that already has a transcript, and the mark
        # appearing halfway down one is not an opening block, it is
        # litter. banner_mod.enabled() is a plain config read (v0.70.0
        # dropped the raster form and the terminal-mode probe that used
        # to pick between it and the drawn one), so this costs nothing
        # boot-critical either way.
        if banner_mod.enabled() and not block_list.children:
            await block_list.mount(BootBanner())
        # Staged-proposal count for the `lore` line (v0.56.0). A socket
        # round trip, and affordable exactly HERE and nowhere else: the
        # opening block is drawn once, before the first prompt, on a pane
        # that has just spent a connect and a git subprocess -- whereas
        # _refresh_status runs on every peer event and every turn-done
        # under the no-per-frame rule GitLine documents. Failure leaves
        # _pending_count None and the line simply omits the fact.
        with contextlib.suppress(Exception):
            lister = getattr(self.engine, "list_pending", None)
            if lister is not None:
                self._pending_count = len(await lister())
        identity = SystemBlock(self._identity_text(git_cwd))
        identity.id = "identity-block"
        await block_list.mount(identity)
        # Startup key notice (v0.96.0): which bound keys THIS terminal
        # cannot deliver, once, right under the identity block a user
        # already reads at session start. No tty check here -- there is
        # nothing to re-derive: unreachable_notice() is empty already on
        # a headless run (keyboard.detect_protocol() short-circuits to
        # UNKNOWN before touching stdin/stdout) and on a kitty-protocol
        # terminal (nothing lost), so this call is the single gate.
        if keyboard_mod.notice_enabled():
            notice = unreachable_notice()
            if notice:
                key_notice = SystemBlock(notice)
                key_notice.id = "key-notice-block"
                await block_list.mount(key_notice)
        if self._boot_report:
            # Item D restore: "restored N tabs, skipped M" -- once, on
            # whichever pane carried the report (doxa.cli picks exactly
            # one), never repeated on a later switch_engine re-boot.
            await block_list.mount(SystemBlock(self._boot_report))
            self._boot_report = None
        self.scroll_transcript_to_end(block_list)
        if self._resume_from:
            # v0.56.0 (/resume): a resumed session must SHOW what it
            # remembers. The model comes back holding the whole
            # conversation (the CLI reloaded it from --resume), and
            # drawing an empty pane over that would leave the user typing
            # into a context they cannot see and have no way to audit --
            # which for a tool whose premise is auditable memory is the
            # wrong failure to ship. So it reuses v0.32.0's machinery
            # outright: same transcript reader, same mount_transcript,
            # same render caps, same on-screen honesty when they bite.
            #
            # The id it reads is THIS session's id, because a resume keeps
            # its id rather than forking a new one (engine._build_options)
            # -- the file being drawn is the file this session is about to
            # go on appending to.
            resume_id, self._resume_from = self._resume_from, None
            with contextlib.suppress(Exception):
                await self._restore_transcript(
                    resume_id, git_cwd, require_backlog_skip=False,
                )
        if self._restore_transcript_wanted:
            self._restore_transcript_wanted = False
            # Suppressed here as well as inside: _note_pane_booted below is
            # what releases DoxaApp's mid-restore persistence guard, so a
            # scrollback that failed to draw must not also cost the user
            # their saved tab set on the NEXT launch.
            with contextlib.suppress(Exception):
                await self._restore_transcript(session_id, git_cwd)
        # Deferred: doxa.app imports this package, so the arrow only
        # points back at call time -- the pane never needs the app
        # class before there is an app.
        from ..app import DoxaApp

        app = self.app
        if isinstance(app, DoxaApp):
            app._note_pane_booted(self)

    async def _peer_pump(self) -> None:
        """Consume the engine's out-of-band stream for the life of the pane:
        peer_message mounts a block immediately (display path only -- the
        model sees it on the next user turn, engine-side); joins/leaves just
        move the status-bar chip; tool_disabled (the gate's two-strikes
        containment) mounts a system block and adds the status-bar
        `⊘ toolname` note. Since the daemon split, TURN events can arrive
        here too -- replayed history right after a reattach, or a turn that
        another attached client of the same daemon is driving -- and render
        into the same TurnBlock/ToolChip widgets a local turn uses.

        ``self.engine`` can legitimately be ``None`` the instant
        ``_engine_ready`` releases this worker, not only after: this task
        is CREATED in ``on_mount`` alongside ``_boot`` (``run_worker``,
        both `group="engine"`/`"peers"`) but is not guaranteed its first
        actual turn on the event loop before ``_boot`` finishes -- and
        ``_boot`` sets ``_session_id`` (what every close-path test in this
        suite waits on before pressing Ctrl+Q/Ctrl+W) BEFORE it sets
        ``_engine_ready`` (a naming-cache lookup sits between the two,
        ``asyncio.to_thread(naming_mod.cached_name, ...)``). A close
        landing in that window calls ``detach()``/``stop()``, both of
        which clear ``self.engine`` immediately and neither of which
        cancels this worker outright -- so by the time ``_engine_ready``
        (already set, from the in-flight ``_boot``) releases the `await`
        below, the engine this worker was about to assert on is already
        gone. Measured: an intermittent ``AssertionError`` out of exactly
        this line, surfaced as a visible in-app error block on an
        otherwise ordinary Ctrl+Q/Ctrl+W. A closed pane has nothing left
        to pump -- returning quietly is the same outcome an ordinary
        worker cancellation would have produced, just reached by a
        different door."""
        await self._engine_ready.wait()
        if self.engine is None:
            return
        async for ev in self.engine.peer_events():
            if ev.type == "peer_message":
                block_list = self.query_one("#block-list", VerticalScroll)
                await block_list.mount(PeerMessageBlock(ev.data))
                self.scroll_transcript_to_end(block_list)
            elif ev.type == "tool_disabled":
                block_list = self.query_one("#block-list", VerticalScroll)
                await block_list.mount(SystemBlock(
                    f"⊘ tool disabled for this session: {ev.data.get('name')}"
                    f" — {ev.data.get('reason')}"
                ))
                self.scroll_transcript_to_end(block_list)
            elif ev.type == "needs_input":
                self._open_needs_input(ev.data)
            elif ev.type == "needs_input_resolved":
                # Some attached client (possibly a DIFFERENT one -- the
                # daemon fans this to everyone, see doxa/client.py) just
                # answered this pane's own pending request. If the popup
                # here is still showing that SAME id, drop it -- it is no
                # longer this pane's to answer.
                popup = self.query_one("#needs-input-popup", NeedsInputPopup)
                if popup.request_id and popup.request_id == ev.data.get("id"):
                    popup.close()
                    self.set_needs_input(False)
            elif ev.type == "derive_done":
                # Streaming deriver (engine-side, DOXA_DERIVE_SECS): newly
                # staged proposals await the SAME human review gate as ever
                # -- this is a notification, never an auto-apply.
                await self._announce_staged(ev.data)
            elif ev.type == "turn_started":
                block_list = self.query_one("#block-list", VerticalScroll)
                self._oob_turn = TurnBlock(str(ev.data.get("prompt") or ""))
                self._oob_chips = {}
                await block_list.mount(self._oob_turn)
                # This client did not drive the turn but IS watching it --
                # same freeze-during-dead-air defect either way, so the
                # elapsed-time ticker (ThinkingMarker.start's own docstring)
                # arms here too, not only in _run_turn below.
                self._oob_turn.thinking.start()
                self.scroll_transcript_to_end(block_list)
                # A new turn starting (even one another client is driving)
                # is itself "seen" -- the same stale-dot clear _run_turn
                # does for a locally-driven turn.
                self._set_tab_class("-done-unseen", False)
            elif ev.type in (
                "text_delta", "reasoning_delta", "tool_call", "tool_result", "turn_done",
            ):
                if self._oob_turn is None and ev.type != "turn_done":
                    # An ORPHANED turn event: something is streaming for a
                    # turn whose turn_started this client never saw. Two
                    # ways to get here, both real -- a reattach that landed
                    # mid-turn, and (before v0.32.0's transcript restore,
                    # still possible for a daemon whose ring head we could
                    # not skip) a replay whose turn_started had already
                    # fallen off the 512-frame ring.
                    #
                    # This used to fall through the `is not None` guard and
                    # DROP the event, silently, which is how a restored tab
                    # rendered as empty next to a live session holding the
                    # whole conversation. An unattributed turn block is a
                    # far smaller lie than no turn at all -- and it says so
                    # in its own header rather than inventing a prompt.
                    block_list = self.query_one("#block-list", VerticalScroll)
                    self._oob_turn = TurnBlock("(turn already in progress)")
                    self._oob_chips = {}
                    await block_list.mount(self._oob_turn)
                    # Genuinely in flight (we just don't know since when) --
                    # ticks from "now", the best estimate available.
                    self._oob_turn.thinking.start()
                    # Same compose wait mount_transcript needs: an event
                    # arriving in the same cycle as the mount would find
                    # the block's Markdown body not yet there.
                    await _composed(self._oob_turn.body, self._oob_turn.tools)
                if self._oob_turn is not None:
                    await self._handle_event(ev, self._oob_turn, self._oob_chips)
                    self.scroll_transcript_to_end()
                    if ev.type == "turn_done":
                        self._oob_turn = None
                        self._oob_chips = {}
            with contextlib.suppress(Exception):
                # A status-bar repaint must never take the pump down with
                # it. This loop is the ONLY renderer of out-of-band traffic
                # (replayed history, a peer's turn, needs_input): an event
                # that lands before this pane's chrome has finished
                # composing used to raise NoMatches here and kill the
                # worker for the life of the tab -- one unlucky moment and
                # the pane went deaf. The refresh is a repaint; skipping
                # one is invisible, and the next event does it again.
                self._refresh_status()

    async def _run_turn(self, prompt: str) -> None:
        # WAIT first, then check -- not the other way round. The assert
        # used to run before the wait, which was only ever true because
        # on_mount set ``self.engine`` synchronously; now the engine is
        # built inside the boot worker (see :meth:`_build_and_boot`), so a
        # prompt submitted during a slow daemon spawn reaches this line
        # with the handle legitimately still None. ``_peer_pump`` already
        # models exactly this window and returns quietly; so does this,
        # for the same reason it gives -- a pane with no engine has
        # nothing to send, and an assertion there would surface a visible
        # error block for a race the user cannot see or avoid.
        await self._engine_ready.wait()
        if self.engine is None:
            return
        self.turn_in_flight = True
        self._set_tab_class("-working", True)
        # A fresh turn starting is itself "seen" -- clear any stale
        # done-unseen dot from a PREVIOUS turn the user has not looked at
        # yet, rather than letting it sit there through a whole new one.
        self._set_tab_class("-done-unseen", False)
        block = TurnBlock(prompt)
        block_list = self.query_one("#block-list", VerticalScroll)
        await block_list.mount(block)
        # The turn genuinely begins here -- arms the per-second elapsed
        # ticker (ThinkingMarker.start's own docstring has the full
        # argument); block.mark_done (both below and on the error path)
        # is what stops it, on every way this turn can end.
        block.thinking.start()
        self.scroll_transcript_to_end(block_list)

        chips: dict[str, ToolChip] = {}

        try:
            async for ev in self.engine.send(prompt):
                await self._handle_event(ev, block, chips)
                self.scroll_transcript_to_end(block_list)
        except Exception as exc:  # noqa: BLE001 -- a refused/broken turn must
            # not take the shell down (e.g. the daemon is busy with another
            # client's turn, or the connection dropped mid-stream).
            await block.mark_done(None, None, True)
            await block_list.mount(SystemBlock(f"turn failed: {exc}"))
            self.scroll_transcript_to_end(block_list)

        self.turn_in_flight = False
        self._set_tab_class("-working", False)
        self._refresh_status()
        # First completed turn of a repo-less session: name the tab from it.
        self._maybe_name_tab(prompt)
        # Queued hunk rejections apply HERE, not in _render_turn_done:
        # that renderer runs while this coroutine is still inside its
        # `async for`, with turn_in_flight still True and this exclusive
        # "turn" worker still the running one -- and applying a rejection
        # STARTS a turn (the agent has to be told), which from inside
        # this worker would cancel the worker doing the telling.
        # call_after_refresh puts it on the message pump instead, one
        # step outside this coroutine's own lifetime.
        self.call_after_refresh(self._flush_diff_rejections)

    def _flush_diff_rejections(self) -> None:
        """Apply whatever the user rejected while this turn was running.

        Runs from the message pump (see the caller): by the time this is
        reached the ``"turn"`` worker group is free, so the rejection's
        own turn can be started without cancelling anything."""
        app = getattr(self, "app", None)
        finder = getattr(app, "diff_pane_for", None)
        if finder is None:
            return
        pane = finder(self._session_id)
        if pane is not None and pane.queued:
            pane.run_worker(pane.flush_pending(), group="reject")
        elif pane is not None:
            # A turn that ended is a turn that may have written; the last
            # tool_result already ticked, but a turn can also end with
            # text after its final edit, and a stale diff is the one
            # thing this surface may not show.
            pane.schedule_refresh()

    async def _handle_event(
        self, ev: EngineEvent, block: TurnBlock, chips: dict[str, ToolChip],
    ) -> None:
        """Render one engine event into ``block``.

        Dispatch, not a chain (v0.34.0): :data:`EVENT_RENDERERS` maps an
        event type to the method that draws it, one method per type. An
        event type with no row is ignored -- exactly what the ``if/elif``
        this replaced did, which had no ``else`` either, because an engine
        that learns a new event type must not be able to crash a client
        that has not learned it yet."""
        renderer = EVENT_RENDERERS.get(ev.type)
        if renderer is None:
            return
        await getattr(self, renderer)(ev, block, chips)

    async def _render_turn_started(
        self, ev: EngineEvent, block: TurnBlock, chips: dict[str, ToolChip],
    ) -> None:
        """Nothing to draw: the block the turn streams into was already
        mounted by whoever opened the turn. The row exists so the event
        type is DECLARED handled rather than falling into the same silence
        an unknown type gets."""
        return

    async def _render_text_delta(
        self, ev: EngineEvent, block: TurnBlock, chips: dict[str, ToolChip],
    ) -> None:
        parent_id = ev.data.get("parent_id") or ""
        parent = chips.get(parent_id)
        if parent is not None:
            # A subagent narrating: trace material, nested under its
            # Task chip -- never mixed into the turn's own prose.
            parent.append_subagent_text(ev.data["text"])
            # The turn's spinner still has to move (v0.56.0): this delta
            # never reaches block.append_text, so without the tick a turn
            # spent entirely inside one Task call would freeze the marker
            # on whatever frame its tool_call left it.
            block.thinking.advance("working")
            # Live routing: an open transcript tab for THIS parent gets
            # the same narration, alongside (not instead of) the chip.
            self._route_transcript_text(parent_id, ev.data["text"])
        else:
            await block.append_text(ev.data["text"])

    async def _render_reasoning_delta(
        self, ev: EngineEvent, block: TurnBlock, chips: dict[str, ToolChip],
    ) -> None:
        parent_id = ev.data.get("parent_id") or ""
        parent = chips.get(parent_id)
        if parent is not None:
            # A subagent's own thinking: no separate reasoning fold
            # exists on a ToolChip (out of scope for this feature --
            # see ReasoningSection's docstring), so it joins the SAME
            # trace buffer its spoken text already uses rather than
            # being dropped on the floor.
            parent.append_subagent_text(ev.data["text"])
            block.thinking.advance("working")  # same reason as above
            self._route_transcript_text(parent_id, ev.data["text"])
        else:
            await block.append_reasoning(ev.data["text"])

    async def _render_tool_call(
        self, ev: EngineEvent, block: TurnBlock, chips: dict[str, ToolChip],
    ) -> None:
        # "working", not "generating" (v0.56.0): between a tool_call and
        # its tool_result no delta arrives, so the glyph genuinely stops
        # moving -- and a frozen spinner labelled "generating" would be
        # claiming text is streaming while a Bash command runs.
        block.thinking.advance("working")
        chip = ToolChip(ev.data["id"], ev.data["name"], ev.data["input"])
        chips[ev.data["id"]] = chip
        parent_id = ev.data.get("parent_id") or ""
        parent = chips.get(parent_id)
        if parent is not None:
            # Trace tree: a subagent's call nests under the Task chip
            # that spawned it, foldable at every level. An unknown
            # parent (ring truncation on replay) degrades to top level
            # -- the call is never dropped.
            await parent.subcalls.mount(chip)
            await self._route_transcript_chip(parent_id, chip)
        else:
            # Top-level chip: compacted behind the turn's ONE "Tool
            # calls (N)" section (see ToolCallsSection/add_tool_chip).
            await block.add_tool_chip(chip)
        if ev.data["name"] == "Task":
            # Subagent tracker: a Task call (top-level or nested -- a
            # subagent's own Task is tracked exactly like a top-level
            # one) starts a new entry in the running registry.
            await self._register_subagent(chip)

    async def _render_tool_result(
        self, ev: EngineEvent, block: TurnBlock, chips: dict[str, ToolChip],
    ) -> None:
        chip = chips.get(ev.data["id"])
        if chip is not None:
            chip.update_result(
                ev.data["result_summary"], ev.data["is_error"],
                ev.data["duration_ms"], image_path=ev.data.get("image_path"),
            )
            self._route_transcript_result(chip)
        if ev.data["id"] in self._subagents:
            await self._unregister_subagent(ev.data["id"])
        # THE TICK for the live diff (v0.92.0). Not a file watcher: DOXA
        # has a documented no-timer, no-per-frame rule and
        # docs/plans/code-graph.md already refused a watcher for the same
        # reason -- a second lifecycle to get wrong. An edit landing IS
        # the event, and this is where an edit lands.
        #
        # The tool INPUT is not on the result event (only the 280-char
        # result summary is), which is why the predicate reads it off the
        # chip: a Bash result alone cannot say whether the command wrote
        # anything, and `chips` is already the call-id -> chip map this
        # method is handed.
        self._tick_diff(
            str(ev.data.get("name") or ""),
            getattr(chip, "tool_input", None),
        )

    def _tick_diff(self, tool_name: str, tool_input: "dict | None") -> None:
        """Tell this session's diff leaf, if it has one open, that the
        tree may have moved. Costs nothing when nobody is looking: no
        diff pane, no query, no git."""
        if not tool_name or not diff_mod.is_tick(tool_name, tool_input):
            return
        app = getattr(self, "app", None)
        finder = getattr(app, "diff_pane_for", None)
        if finder is None:
            return
        pane = finder(self._session_id)
        if pane is not None:
            pane.schedule_refresh()

    async def _render_turn_done(
        self, ev: EngineEvent, block: TurnBlock, chips: dict[str, ToolChip],
    ) -> None:
        # Same tier lookup _refresh_status/_usage_text already do --
        # keeps the per-turn figure consistent with both (item T).
        account = getattr(self.engine, "account", None) or {}
        tier = identity_mod.account_tier(account)
        await block.mark_done(
            ev.data.get("cost_usd"), ev.data.get("duration_ms"),
            ev.data.get("is_error", False), tier,
        )
        # The one place the headroom chip is recomputed: a turn just
        # spent budget, and the CLI may have refreshed its own cache.
        self._refresh_usage_chip()
        self._refresh_status()
        self._on_turn_done_status(ev.data.get("duration_ms"))

    # -- subagent tracker (queue item 4) ------------------------------

    async def _register_subagent(self, chip: "ToolChip") -> None:
        """One Task call started: add it to the running registry, then
        sync the second line and the status chip -- both read len() of
        the same dict, so this one write keeps them both correct."""
        self._subagents[chip.call_id] = chip
        await self._sync_subagent_line()
        self._refresh_status()

    async def _unregister_subagent(self, call_id: str) -> None:
        """That Task call's own tool_result just landed: it drops out of
        the running registry (the second line and the status chip shrink
        by one, possibly to zero) -- but an OPEN transcript tab for it
        stays open, just marked done."""
        self._subagents.pop(call_id, None)
        await self._sync_subagent_line()
        self._refresh_status()
        tab = self._transcript_tabs.get(call_id)
        if tab is not None:
            tab.mark_done()

    async def _sync_subagent_line(self) -> None:
        """Mount the second line on the first running subagent, unmount it
        on the last one finishing -- mount/unmount, never a display toggle,
        so an idle pane (the common case) carries zero cost for a feature
        it isn't using right now. While mounted its content is rewritten
        on every registry change (cheap: one markup string, no repaint
        storm any worse than a status-bar update already is)."""
        if self._subagents and self._subagent_line is None:
            self._subagent_line = SubagentLine(self)
            with contextlib.suppress(Exception):
                status_bar = self.query_one("#status-bar", Static)
                await self.mount(self._subagent_line, after=status_bar)
        elif not self._subagents and self._subagent_line is not None:
            line, self._subagent_line = self._subagent_line, None
            with contextlib.suppress(Exception):
                await line.remove()
        if self._subagent_line is not None:
            self._subagent_line.refresh_labels([
                (call_id, _subagent_label(chip))
                for call_id, chip in self._subagents.items()
            ])

    async def open_transcript(self, call_id: str) -> None:
        """Open (or, if it is already open, just focus) the read-only
        transcript tab for one RUNNING subagent -- the only way in here is
        a click on the second line, which only ever offers ids currently
        in ``self._subagents``, so a miss (finished and dropped between
        the click and this running) degrades to a silent no-op rather than
        a crash.

        Focus moves to the new tab's own scroll container (it is
        focusable, so the arrow keys/PageUp/PageDown a reader would reach
        for just work) -- load-bearing, not just nicety: TabbedContent's
        own ``_on_tab_pane_focused`` snaps ``.active`` back to whichever
        pane holds the CURRENTLY focused widget, and this pane's own
        ``#prompt-input`` stays focused (its tab merely hides, focus does
        not move on its own) unless something claims focus in the pane
        being switched to -- exactly what SessionPane's own boot already
        does for itself by focusing its prompt input on mount."""
        # THIS pane's own group's strip (v0.97.0) -- a subagent transcript
        # belongs beside the session that spawned it, never in whichever
        # group happens to hold the keyboard. Through v0.95.0 there was one
        # strip and the distinction did not exist.
        from ..ui import split as split_mod

        tabbed = split_mod.tabbed_of(self)
        if tabbed is None or not tabbed.is_mounted:
            return  # app mid-teardown, or this pane is not in a group yet
        existing = self._transcript_tabs.get(call_id)
        if existing is not None:
            tabbed.active = existing.id or tabbed.active
            existing.scroll.focus()
            return
        chip = self._subagents.get(call_id)
        if chip is None:
            return
        label = _subagent_label(chip)
        tab = SubagentTranscriptTab(
            call_id, label, self, id=f"trace-{self.id}-{call_id}",
        )
        self._transcript_tabs[call_id] = tab
        await tabbed.add_pane(tab)
        await tab.replay(chip)
        tabbed.active = tab.id or tabbed.active
        tab.scroll.focus()

    def _route_transcript_text(self, parent_id: str, text: str) -> None:
        tab = self._transcript_tabs.get(parent_id)
        if tab is not None:
            tab.append_narration(text)

    async def _route_transcript_chip(self, parent_id: str, chip: "ToolChip") -> None:
        tab = self._transcript_tabs.get(parent_id)
        if tab is not None:
            await tab.mirror_chip(chip)

    def _route_transcript_result(self, chip: "ToolChip") -> None:
        """A tool_result may belong to a chip mirrored inside some open
        transcript tab (a direct child of the tab's own subagent) -- find
        it by call id and bring the mirror's own result up to date too.
        At most one tab can hold a mirror for a given id in practice (ids
        are the SDK's own tool_use ids), so the first match wins."""
        for tab in self._transcript_tabs.values():
            mirror = tab.mirror_chips.get(chip.call_id)
            if mirror is not None:
                mirror.update_result(
                    chip.tool_result, chip.is_error, chip.duration_ms,
                    image_path=chip.tool_image_path,
                )
                break

    # -- needs-input dialog (queue item 5) -----------------------------

    def _open_needs_input(self, data: dict) -> None:
        """A fresh needs_input event: open the dialog, blink the tab
        (cleared on answer or on activating this tab -- set_needs_input's
        own, already-tested convention, unchanged here), and notify --
        gated by THIS pane's real app_has_focus (the detached-daemon
        case, no client at all attached, is handled separately,
        daemon-side -- see doxa/daemon.py's _peer_pump). This is the ONE
        notification-worthy turn outcome (v0.85.0): a turn merely
        FINISHING is not, and nothing fires for that -- see
        doxa.notify's module docstring.

        Claims the keyboard, if this pane is the tab the user is actually
        looking at (v0.43.0). The dialog is driven entirely through
        ``PromptInput``'s key protocol -- it is ``can_focus = False`` by
        design -- so a dialog opening while focus sits on the transcript
        or the tab strip is a dialog no key can answer, and this one
        BLOCKS the session until it is. That makes "something is waiting
        on you" exactly the explicit intent v0.38.0 says a focus move
        needs; it does not weaken that rule, it names one more site.

        Only when this pane is ACTIVE. Focusing a widget inside a
        ``TabPane`` also activates that pane, so doing it unconditionally
        would yank a background request's tab out from under someone
        typing in another one -- the blink is that case's whole signal,
        and ``DoxaApp._focus_tab`` focuses the prompt when they come over
        to answer."""
        popup = self.query_one("#needs-input-popup", NeedsInputPopup)
        popup.ask(data)
        self.set_needs_input(True)
        if getattr(self.app, "active_pane", None) is self:
            with contextlib.suppress(Exception):
                self.query_one("#prompt-input", PromptInput).focus()
        notify_mod.notify_needs_input(
            getattr(self.app, "app_has_focus", True),
            self.display_name(),
            _needs_input_summary(data),
        )

    # -- staged proposals (v0.31.0) ------------------------------------

    async def _announce_staged(self, data: dict) -> None:
        """One ``derive_done`` event, made reachable from wherever the user
        actually is. Before v0.31.0 this was a single count-only
        :class:`SystemBlock` in ONE pane's block list, pointing at
        ``/lore:pending`` -- a Claude Code plugin command that does not
        exist inside DOXA -- so a background reviewer that found something
        was invisible unless you happened to be looking at that pane, and
        the hint it printed led nowhere. Three surfaces now, all fed by
        the same event:

        * the transcript block, which QUOTES what was staged (ellipsized
          per row by ``doxa.engine.staged_event_payload``, which also
          scrubs it and bounds it to a wire frame) and says how many rows
          it left out, because a count alone cannot tell you whether a
          batch is worth opening;
        * the tab, via :meth:`set_staged` -- a calm steady tint, never the
          needs-input blink (see that method for why);
        * a desktop notification, focus-gated exactly like every other
          trigger (``doxa.notify.notify_staged``) so it can only ever
          reach you when you are NOT already looking at DOXA.

        The block's trailing line is a live click target onto the same
        ``/pending`` list the command opens -- an announcement that names a
        destination should be able to take you there.
        """
        staged = int(data.get("staged") or 0)
        if staged <= 0:
            return
        texts = [str(t) for t in (data.get("texts") or [])]
        omitted = int(data.get("omitted") or 0)
        noun = "proposal" if staged == 1 else "proposals"
        lines = [f"{staged} {noun} staged by the background reviewer"]
        # Model-derived text on a block that carries a click action: escape
        # it, per SystemBlock's own contract and _escape_markup's docstring.
        lines += [f"  • {_escape_markup(text)}" for text in texts]
        if omitted > 0:
            lines.append(f"  … and {omitted} more")
        if not texts and staged > 0:
            # The count is real but the preview is empty (an event that
            # crossed the daemon socket from an older peer, or proposals
            # that scrubbed down to nothing). Say so rather than showing a
            # bare number as if that were the whole story.
            lines.append("  (no preview available — open the list to read them)")
        block_list = self.query_one("#block-list", VerticalScroll)
        await block_list.mount(SystemBlock(
            "\n".join(lines),
            link_label="/pending — review them",
            on_link=lambda: self.run_worker(
                self.open_pending_picker(), group="command"
            ),
        ))
        self.scroll_transcript_to_end(block_list)
        self.set_staged(True)
        notify_mod.notify_staged(
            getattr(self.app, "app_has_focus", True),
            self.display_name(),
            staged,
            texts,
        )

    async def _resolve_needs_input(
        self, popup: "NeedsInputPopup", index: "int | None", decline: bool,
    ) -> None:
        """Answer (or decline) whatever the popup currently holds, and
        tell the engine -- SessionEngine and EngineClient both expose
        ``answer_needs_input`` (see doxa/client.py's engine-parity note),
        so this reads the same regardless of the daemon split. A stale
        popup (already closed -- e.g. a needs_input_resolved from another
        client beat this keystroke) is a silent no-op, same discipline
        every other "the widget might already be gone" call site in this
        pane follows."""
        if not popup.is_open:
            return
        request_id = popup.request_id
        if decline:
            answer = (
                {"declined": True} if popup.kind == "ask_user"
                else {"decision": "deny"}
            )
            popup.close()
        else:
            assert index is not None
            if not popup.choose_index(index):
                return  # ask_user: more questions to go -- stays open
            answer = popup.answer_payload()
            popup.close()
        self.set_needs_input(False)
        # Refresh NOW: this path runs off a key/click worker, never
        # through _peer_pump's own trailing _refresh_status() call -- the
        # engine's matching needs_input_resolved event will ALSO loop back
        # through that pump shortly (in-process, or fanned out by the
        # daemon), but the status-bar hint and tab class must not wait on
        # that round-trip to catch up.
        self._refresh_status()
        engine = self.engine
        if engine is not None and request_id:
            answerer = getattr(engine, "answer_needs_input", None)
            if answerer is not None:
                try:
                    await answerer(request_id, answer)
                except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
                    # v0.56.0. This was `contextlib.suppress(Exception)`,
                    # and it is one of the four defects that motivated the
                    # error surface: "the needs-input dialog stopped
                    # answering keys". The popup has ALREADY closed and
                    # set_needs_input(False) has ALREADY run by the time
                    # this line is reached, so a failed delivery left the
                    # agent blocked forever on a question the user HAD
                    # answered -- with no dialog, no message and no sign
                    # anything had gone wrong. A wedged session that looks
                    # exactly like an idle one.
                    #
                    # Now it is an error block plus a line saying what to
                    # do about it, which is the difference between "DOXA is
                    # broken" and "answer it again".
                    reporter = getattr(self.app, "report_exception", None)
                    if reporter is not None:
                        reporter(
                            exc,
                            context="delivering your answer to the session",
                        )
                    await self._system(
                        "your answer did not reach the session — it is still "
                        "waiting. What failed is in the block above; answer "
                        "again when it reappears, or /detach and reattach."
                    )

    def _on_turn_done_status(self, duration_ms: "float | None") -> None:
        """Tab-status side effect of ONE finished turn. Reached for a turn
        THIS client drove (_run_turn) and for one replayed in from another
        attached client of the same daemon (_peer_pump's turn_started/
        turn_done forwarding) -- both funnel through _handle_event's
        turn_done branch, and both are equally "a turn just finished on a
        session you might not be looking at".

        No desktop notification here, on purpose, since v0.85.0: a turn
        finishing is not a notification-worthy event -- reported verbatim,
        "response finished should not trigger a desktop notification.
        Only when user input is required." The done-unseen tab tint below
        is still exactly right for this (a quiet, in-window "you missed
        something" marker); the desktop banner only fires from
        :meth:`_open_needs_input`, the one place a turn is actually
        BLOCKED on the user rather than merely over. ``duration_ms`` is
        kept in the signature even though nothing here reads it anymore --
        every caller already has it in hand off the same event, and a
        churned signature would be a second, purely mechanical diff for no
        behavioral reason."""
        active = self.app.active_pane is self
        if not active:
            self._set_tab_class("-done-unseen", True)

    async def switch_engine(self, make_engine: "Callable[[], Any]") -> None:
        """Swap this pane's live engine handle: detach/finalize the old one,
        build the new one (off-loop -- a daemon spawn blocks on
        subprocess+registry polling), reset the block list, and restart the
        boot + pump workers (both exclusive in their pane-scoped groups, so
        the old pump dies with its engine)."""
        old, self.engine = self.engine, None
        self._engine_ready = asyncio.Event()
        self._oob_turn = None
        self._oob_chips = {}
        if old is not None:
            with contextlib.suppress(Exception):
                await old.finalize()
        try:
            self.engine = await asyncio.to_thread(make_engine)
        except Exception as exc:  # noqa: BLE001 -- spawn/attach failure must surface, not crash
            block_list = self.query_one("#block-list", VerticalScroll)
            await block_list.mount(SystemBlock(f"session switch failed: {exc}"))
            return
        await self.query_one("#block-list", VerticalScroll).remove_children()
        self.query_one("#status-bar", Static).update("doxa · connecting…")
        self.run_worker(self._boot(), exclusive=True, group="engine")
        self.run_worker(self._peer_pump(), exclusive=True, group="peers")

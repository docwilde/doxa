# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.ui.diffview -- the widget half of the live diff.

:class:`DiffPane` is a LEAF of the layout tree, not a tab and not a
region inside a session pane. That is the whole point of the feature and
it is the design check ``docs/plans/live-diff.md`` asks of v0.91.0's
split: *session left, diff right, both live*. Being a real leaf is what
buys, for free and without a second implementation:

* **it keeps rendering while focus is in the session** -- v0.91.0 settled
  "visible and focused are different states", and a leaf that only
  painted when focused would have been the bug that rule exists to
  prevent;
* **the divider is already adjustable** -- ``Alt+←``/``Alt+→``
  (``DoxaApp.grow_pane_towards``) moves the boundary between any two
  leaves, which is exactly the "sibling gesture" the spec asks for and
  which v0.91.0 had already shipped;
* **directional focus already reaches it** -- ``Ctrl+Shift+→`` lands on
  it because :meth:`DoxaApp._pane_regions` reads painted rectangles, not
  a widget type.

What it did NOT get for free is in :mod:`doxa.layout`: through v0.91.0 a
``Leaf`` was a SESSION and nothing else, and :func:`doxa.ui.split._leaf_of`
returned ``None`` for anything that was not a ``SessionPane`` -- so a
diff leaf would have been dropped from ``PaneTab.tree()``, meaning the
persisted record would have said "one pane" while the screen showed two.
:attr:`doxa.layout.Leaf.view` is the one field that closes that, and
:meth:`layout_leaf` is how this widget answers for itself rather than
having ``split.py`` learn about diffs.

**No timer, no per-frame work.** The pane recomputes when
:meth:`schedule_refresh` is called and at no other time, and its one
caller is the tool-result stream (:mod:`doxa.session.runtime`). An edit
landing IS the tick.
"""

from __future__ import annotations

import asyncio
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, Collapsible, Input, Static

from .. import diff as diff_mod
from .. import layout as layout_mod

#: Rows of a hunk rendered before the hunk itself folds. A hunk longer
#: than a screenful is still a hunk you reject as one unit, so this
#: truncates the VIEW and says so -- it never splits the patch, which
#: would make the reject button reverse something other than what was on
#: screen.
MAX_HUNK_ROWS = 400


def _hunk_text(hunk: "diff_mod.Hunk") -> Text:
    """One hunk as coloured text, unified.

    Rich ``Text`` rather than console markup: a diff body is arbitrary
    source, and source contains ``[`` -- markup would try to parse it.
    v0.28.0's lesson in a smaller key."""
    out = Text()
    rows = hunk.lines[:MAX_HUNK_ROWS]
    for line in rows:
        mark = line[:1]
        if mark == "+":
            out.append(line + "\n", style="#7FB069")
        elif mark == "-":
            out.append(line + "\n", style="#D08770")
        elif mark == "\\":
            out.append(line + "\n", style="#6E6459 italic")
        else:
            out.append(line + "\n", style="#B4AB9E")
    if len(hunk.lines) > MAX_HUNK_ROWS:
        out.append(
            f"… {len(hunk.lines) - MAX_HUNK_ROWS} more lines not shown "
            "(the hunk is whole; only this view is cut)\n",
            style="#8A8073 italic",
        )
    return out


def _side_by_side_text(hunk: "diff_mod.Hunk", width: int) -> Text:
    """The same hunk in two columns, old on the left, new on the right.

    Only ever reached above :data:`doxa.diff.SIDE_BY_SIDE_MIN_COLS` --
    see :func:`doxa.diff.side_by_side_allowed` for why the threshold is
    where it is. Removed and added runs are paired positionally, which is
    what every two-column diff does and is honest as long as the pairing
    is never claimed to be a word-level match: this is a layout, not a
    second differ."""
    col = diff_mod.split_columns(width)
    out = Text()

    def row(left: str, right: str, lstyle: str, rstyle: str) -> None:
        out.append(f"{left[:col]:<{col}}", style=lstyle)
        out.append("│", style="#4A443C")
        out.append(f"{right[:col]:<{col}}\n", style=rstyle)

    removed: "list[str]" = []
    added: "list[str]" = []

    def flush() -> None:
        for i in range(max(len(removed), len(added))):
            left = removed[i] if i < len(removed) else ""
            right = added[i] if i < len(added) else ""
            row(
                left, right,
                "#D08770" if left else "#3A342E",
                "#7FB069" if right else "#3A342E",
            )
        removed.clear()
        added.clear()

    for line in hunk.lines[:MAX_HUNK_ROWS]:
        mark, body = line[:1], line[1:]
        if mark == "-":
            removed.append(body)
        elif mark == "+":
            added.append(body)
        else:
            flush()
            if mark == "\\":
                continue
            row(body, body, "#B4AB9E", "#B4AB9E")
    flush()
    return out


class HunkView(Vertical):
    """One hunk, with the one action this feature has.

    "Reject or keep" -- there is no edit box for the patch itself, on
    purpose: editing a hunk by hand belongs in an editor and a
    half-editor is worse than none. The reason field is not an editor,
    it is the sentence the agent is told, and it is worth far more than
    a bare revert because it is what stops the agent re-making the same
    edit."""

    DEFAULT_CSS = """
    HunkView { height: auto; margin-bottom: 1; }
    HunkView > Static.hunk-head { color: #8A8073; }
    HunkView > Static.hunk-pending { color: #E0B341; }
    HunkView > Horizontal { height: auto; }
    HunkView Input { width: 1fr; }
    HunkView Button { width: auto; min-width: 10; }
    """

    def __init__(
        self,
        file_diff: "diff_mod.FileDiff",
        hunk: "diff_mod.Hunk",
        width: int = 0,
    ) -> None:
        super().__init__()
        self.file_diff = file_diff
        self.hunk = hunk
        # The width to paint at on mount. Carried in rather than read off
        # ``self.size`` there, because a widget's size is zero until it
        # has been laid out -- so a hunk built lazily would paint unified
        # in a pane wide enough for two columns and stay that way until
        # the next resize.
        self._width = width
        self._body = Static("", classes="hunk-body")
        self._pending = Static("", classes="hunk-pending")
        self._pending.display = False  # hide-at-zero, the house convention
        self._reason = Input(placeholder="reason (optional)", classes="hunk-reason")
        self._button = Button("reject", variant="warning", classes="hunk-reject")

    def compose(self) -> ComposeResult:
        yield Static(self.hunk.label, classes="hunk-head")
        yield self._body
        yield self._pending
        with Horizontal():
            yield self._reason
            yield self._button

    def on_mount(self) -> None:
        self.paint(self._width)

    def paint(self, width: int) -> None:
        """Render the body at this width. Called on mount and whenever
        the pane is repainted, because the unified/side-by-side choice is
        a function of the width the pane actually has RIGHT NOW -- the
        same "fit it to the box it is painted into" idiom
        ``TurnBlock._title_budget`` uses."""
        if diff_mod.side_by_side_allowed(width):
            self._body.update(_side_by_side_text(self.hunk, width))
        else:
            self._body.update(_hunk_text(self.hunk))

    @property
    def reason(self) -> str:
        return self._reason.value

    def mark_pending(self, note: str) -> None:
        """Show that this rejection is queued and not yet applied.

        The spec's own reason for choosing queue-until-``turn_done`` over
        the two alternatives: a rejection the user has clicked and cannot
        see the effect of is the worst of the three outcomes. So the
        badge is not decoration, it is the half of the decision that
        makes it defensible."""
        self._pending.update(note)
        self._pending.display = True
        self._button.disabled = True

    def clear_pending(self) -> None:
        self._pending.display = False
        self._pending.update("")
        self._button.disabled = False


class FileSection(Collapsible):
    """One file, collapsed by default, with its changed-line counts on the
    fold -- the pattern ``ToolCallsSection`` established and ``ToolChip``
    refined with lazy formatting. A twenty-file diff must not be a wall,
    and forty hunks nobody expanded must not be forty mounted widget
    trees either."""

    def __init__(self, file_diff: "diff_mod.FileDiff") -> None:
        self.file_diff = file_diff
        self._built = False
        self._hunks = Vertical(classes="diff-hunks")
        super().__init__(self._hunks, title=self._title(), collapsed=True)

    def _title(self) -> str:
        return f"◈ {self.file_diff.summary()}"

    def build(self, width: int, passes: int = 3) -> None:
        """Mount this file's hunks. First expand only -- the same
        ``format_body`` discipline ``ToolChip`` uses, and mounted without
        awaiting for the same reason it is there: this runs from a
        synchronous ``Collapsible.Expanded`` handler.

        Deferred while the hunk container is not mountable yet. A
        ``Collapsible`` is handed its contents in ``__init__``, so
        ``self._hunks`` exists from the first line of this section's life
        and is mounted only when the section itself composes -- and
        :meth:`DiffPane._remark_queued` calls this on a section it JUST
        mounted, which is exactly that window. Measured as ``MountError:
        Can't mount widget(s) before Vertical(classes='diff-hunks') is
        mounted``, from a background task, in one full-suite run and not
        in the targeted ones: the same race v0.91.0 met in
        ``SessionPane._system``, met again from a widget that has no
        transcript to drop a block into. Retried rather than dropped,
        because an unbuilt section is a fold that opens onto nothing.

        BOUNDED, since v0.95.0. This retry and :meth:`DiffPane._repaint`'s
        were the only two ``call_after_refresh`` loops in the codebase
        that could reschedule themselves forever: each one calls back into
        the method that scheduled it, with no counter and no delay, so a
        container that never becomes mountable would spin the message pump
        at full speed and never let the app go idle. That is the exact
        shape v0.91.0 already had to remove once (an unbounded focus
        retry), and their own sibling :meth:`DiffPane._apply_badges` has
        carried a pass counter from the day it was written -- the omission
        here reads as an oversight rather than a decision. Neither could
        be provoked in measurement; both are bounded now anyway, because
        "cannot currently be triggered" is not a property a retry loop
        keeps on its own."""
        if self._built:
            self.repaint(width)
            return
        if not self._hunks.is_mounted:
            if passes > 1:
                self.call_after_refresh(self._build_later, width, passes - 1)
            return
        self._built = True
        if self.file_diff.skipped:
            self._hunks.mount(
                Static(
                    f"not rendered — {self.file_diff.skipped}",
                    classes="diff-skipped",
                )
            )
            return
        for hunk in self.file_diff.hunks:
            self._hunks.mount(HunkView(self.file_diff, hunk, width))

    def _build_later(self, width: int, passes: int = 3) -> None:
        """The retry :meth:`build` schedules. Silent if this section left
        the DOM in the meantime -- there is then nothing to build into,
        and that is the one case where dropping IS the right answer -- and
        silent after ``passes`` attempts, which is the other."""
        if self.is_mounted:
            self.build(width, passes)

    def repaint(self, width: int) -> None:
        for view in self._hunks.query(HunkView):
            view.paint(width)


class DiffPane(Vertical):
    """The diff, live, as one leaf of a tab's layout tree.

    Holds no session and drives no engine: it holds a session ID and the
    worktree path that session is running in, and it renders what ``git
    diff`` says about that path. "Not a second source of truth" is the
    spec's phrasing and this is the shape of obeying it."""

    #: A leaf has to be focusable or directional focus cannot land on it,
    #: and a diff you cannot scroll with the keyboard is a diff you
    #: cannot read past the first screen.
    can_focus = True

    DEFAULT_CSS = """
    DiffPane { height: 1fr; width: 1fr; }
    DiffPane > Static.diff-head { height: auto; padding: 0 1; color: #C9C0B2; }
    DiffPane > Static.diff-note { height: auto; padding: 0 1; color: #E0B341; }
    DiffPane > VerticalScroll { height: 1fr; }
    DiffPane .diff-skipped { color: #8A8073; }
    """

    def __init__(
        self, session_id: str, cwd: str, *, id: "str | None" = None
    ) -> None:
        super().__init__(id=id)
        self.session_id = session_id
        self.diff_cwd = cwd
        self.result: "diff_mod.DiffResult" = diff_mod.DiffResult(
            status=diff_mod.STATUS_OK, base="", detail=""
        )
        #: Rejections clicked while a turn was in flight. Applied by
        #: :meth:`flush_pending`, whose one caller is the tail of
        #: ``SessionPane._run_turn``.
        self.queued: "list[diff_mod.PendingRejection]" = []
        self._head = Static("reading the diff…", classes="diff-head")
        self._note = Static("", classes="diff-note")
        self._note.display = False
        self._files = VerticalScroll(id=f"diff-files-{id or session_id}")
        self._painted = False

    # -- layout -------------------------------------------------------

    def layout_leaf(self) -> "layout_mod.Leaf":
        """This pane as a layout-tree leaf.

        Duck-typed rather than an ``isinstance`` arm in
        :func:`doxa.ui.split._leaf_of`: the widget layer already refuses
        to know what a session is (``split.py`` imports ``SessionPane``
        lazily, inside functions, precisely to avoid the cycle), and
        making it also know what a diff is would be the second half of a
        mistake. A leaf answers for itself."""
        return layout_mod.Leaf(
            session_id=self.session_id,
            cwd=self.diff_cwd,
            view=layout_mod.VIEW_DIFF,
        )

    def compose(self) -> ComposeResult:
        yield self._head
        yield self._note
        yield self._files

    async def on_mount(self) -> None:
        # After the next refresh, not now. A leaf created at runtime is
        # mounted into a SplitBox that was made empty ahead of time, so
        # this pane's own ``compose`` children are still on their way in
        # when ``on_mount`` fires -- measured, and it is the same window
        # v0.91.0 hit from the other side (``SessionPane._system``'s
        # MountError). Deferring one refresh is the difference between a
        # first paint and a pane that says "reading the diff…" forever.
        self.call_after_refresh(self.schedule_refresh)

    # -- the tick -----------------------------------------------------

    def schedule_refresh(self) -> None:
        """Recompute, from wherever the tick came from.

        ``exclusive=True`` on its own group is the whole rate limit, and
        it is the right one: a turn that lands thirty edits fires thirty
        ticks, each cancelling the last in-flight git call, and the user
        sees the diff after the last edit rather than a queue of thirty
        stale ones. No timer, no debounce interval to tune, no second
        lifecycle -- the same reasoning that gave v0.56.0's spinner zero
        idle cost."""
        if not self.is_mounted:
            return
        self.run_worker(self.refresh_diff(), exclusive=True, group="diff")

    async def refresh_diff(self) -> None:
        """Run ``git diff`` off the event loop and repaint.

        ``asyncio.to_thread`` for the reason ``SessionEngine.switch_branch``
        gives at its own call: these are git subprocess calls, and a
        keystroke must not wait behind one."""
        try:
            result = await asyncio.to_thread(diff_mod.compute, self.diff_cwd)
        except Exception as exc:  # noqa: BLE001 -- a broken diff is a message,
            # never a dead pane: this runs from a worker, and an escaping
            # exception there is an error block nobody claimed.
            result = diff_mod.DiffResult(
                status=diff_mod.STATUS_ERROR, detail=str(exc)
            )
        self.result = result
        await self._repaint()

    async def _repaint(self, passes: int = 3) -> None:
        """Rebuild the file list.

        Guarded on ``is_mounted`` as well as ``NoMatches``, and for the
        v0.91.0 reason ``SessionPane._system`` spells out: ``query_one``
        SUCCEEDS for a node that is in the DOM but not yet mountable, and
        it is ``mount`` that raises ``MountError``. This runs from a
        worker, so it can land in exactly that window.

        ``passes`` bounds the retry -- see :meth:`FileSection.build` for
        why these two grew a counter in v0.95.0."""
        if not self.is_mounted:
            return
        try:
            files = self.query_one(f"#{self._files.id}", VerticalScroll)
        except NoMatches:
            files = None
        if files is None or not files.is_mounted or not self._head.is_mounted:
            # BOTH conditions, in that order, for the reason
            # ``SessionPane._system`` spells out: ``query_one`` SUCCEEDS
            # for a node that is in the DOM but not yet mountable, and it
            # is ``mount`` that raises ``MountError``. Unlike a dropped
            # system block, though, a dropped repaint is not harmless --
            # it is a pane frozen on its placeholder -- so this retries
            # once the pump has caught up rather than returning silently.
            if passes > 1:
                self.call_after_refresh(self._repaint_later, passes - 1)
            return
        self._head.update(self.result.headline())
        notes = [self.result.truncated] if self.result.truncated else []
        if self.queued:
            notes.append(
                f"{len(self.queued)} rejection(s) queued until this turn ends"
            )
        self._note.update("  ·  ".join(notes))
        self._note.display = bool(notes)
        # Remembered across the rebuild: which files were open, so an
        # edit landing mid-read does not fold the file you were reading.
        # By path, not by widget -- the widgets are about to be replaced.
        open_paths = {
            s.file_diff.path for s in files.query(FileSection)
            if not s.collapsed
        }
        await files.remove_children()
        width = self.size.width or 0
        for file_diff in self.result.files:
            section = FileSection(file_diff)
            await files.mount(section)
            if file_diff.path in open_paths:
                section.collapsed = False
        self._painted = True
        self._repaint_open(width)
        self._remark_queued(width)

    def _remark_queued(self, width: int) -> None:
        """Put the pending badges back after a rebuild.

        The badge lives on a ``HunkView`` and every tick replaces the
        ``HunkView``s, so without this a queued rejection would be
        "visibly marked" only until the agent's next edit -- which is
        precisely the interval during which it is queued. Since the whole
        argument for queueing over refusing is that the user can SEE the
        pending state, a badge that expires on the next tick would leave
        the choice with none of its justification. The file is expanded
        for the same reason: a badge inside a fold nobody opened is not a
        badge."""
        if not self.queued:
            return
        paths = {item.path for item in self.queued}
        for section in self._files.query(FileSection):
            if section.file_diff.path in paths:
                section.collapsed = False
                section.build(width)
        # The badges land on the NEXT pass: ``build`` mounts, and a
        # Textual mount is not visible to ``query`` until the pump has
        # run. Measured the same way the first-paint deferral above was.
        self.call_after_refresh(self._apply_badges)

    def _apply_badges(self, passes: int = 3) -> None:
        """Mark every queued hunk that is on screen, and come back for
        the ones that are not yet.

        ``FileSection.build`` can itself have been deferred (its hunk
        container was not mountable), so one pass is not enough to
        promise the badge landed -- and the badge is the whole
        justification for queueing. Bounded at three passes rather than
        looped: if a hunk is still not there by then it is not coming
        back (the file was renamed out from under the queue), and the
        note above the file list still carries the count."""
        if not self.queued or not self.is_mounted:
            return
        wanted = {(item.path, item.hunk.header): item for item in self.queued}
        marked = 0
        for view in self._files.query(HunkView):
            item = wanted.get((view.file_diff.path, view.hunk.header))
            if item is not None:
                view.mark_pending(item.mark())
                marked += 1
        if marked < len(wanted) and passes > 1:
            self.call_after_refresh(self._apply_badges, passes - 1)

    def _repaint_later(self, passes: int = 3) -> None:
        """The retry :meth:`_repaint` schedules when its container was
        not mountable yet. A worker rather than a direct call because
        ``_repaint`` is a coroutine and this runs from the message pump."""
        if self.is_mounted:
            self.run_worker(self._repaint(passes), group="diff-paint")

    def _repaint_open(self, width: int) -> None:
        for section in self._files.query(FileSection):
            if not section.collapsed:
                section.build(width)

    def on_resize(self) -> None:
        """A width change can cross :data:`doxa.diff.SIDE_BY_SIDE_MIN_COLS`
        in either direction. Repainting the OPEN sections is cheap (the
        hunks are already parsed; this is a ``Static.update``) and it is
        the only way an Alt+arrow drag can change the view it was aimed
        at. Nothing is recomputed -- git is not called from here."""
        if self._painted:
            self._repaint_open(self.size.width or 0)

    @on(Collapsible.Expanded)
    def _on_file_expanded(self, event: Collapsible.Expanded) -> None:
        if isinstance(event.collapsible, FileSection):
            event.stop()
            event.collapsible.build(self.size.width or 0)

    # -- reject -------------------------------------------------------

    @on(Button.Pressed)
    def _on_reject(self, event: Button.Pressed) -> None:
        event.stop()
        node: Any = event.button
        while node is not None and not isinstance(node, HunkView):
            node = node.parent
        if node is None:
            return
        self.run_worker(self.reject(node), group="reject")

    async def reject(self, view: HunkView) -> None:
        """Reject one hunk: the file goes back, and the agent is told.

        **Both, in that order, and atomically from the user's point of
        view.** Doing only the revert leaves the agent confidently wrong
        -- it patches against a premise that is no longer true. Doing
        only the message leaves the bad code in the tree until the agent
        gets round to it. The order matters because the message says the
        change is already gone, and a message that says so before it is
        true is a lie the agent will act on.

        A turn in flight sends this down the queue instead, and the
        reason is not only that reverting under a mid-edit agent produces
        a conflict neither side understands: the daemon REFUSES a second
        concurrent prompt outright (``doxa.daemon._handle_prompt``:
        "a turn is already running in this session"), so the message half
        of the pair could not be delivered even if the revert half
        landed. Applying immediately is not a race this app could win."""
        pane = self.session_pane()
        if pane is not None and getattr(pane, "turn_in_flight", False):
            item = diff_mod.PendingRejection(
                path=view.file_diff.path,
                hunk_label=view.hunk.label,
                reason=view.reason,
                file_diff=view.file_diff,
                hunk=view.hunk,
            )
            self.queued.append(item)
            view.mark_pending(item.mark())
            self._note.update(
                f"{len(self.queued)} rejection(s) queued until this turn ends"
            )
            self._note.display = True
            return
        outcome = await asyncio.to_thread(
            diff_mod.revert_hunk, self.diff_cwd, view.file_diff, view.hunk
        )
        if not outcome.applied:
            # "say so and change nothing" -- never force. The message goes
            # to the SESSION's transcript, not a toast: the pane it is
            # about is the one beside this one.
            await self._tell_user(outcome.message)
            return
        await self._tell_agent(view.file_diff, view.hunk, view.reason)
        await self.refresh_diff()

    async def flush_pending(self) -> None:
        """Apply everything queued. Called once, from the tail of
        ``SessionPane._run_turn``.

        NOT from ``_render_turn_done``: that fires while ``_run_turn`` is
        still inside its ``async for``, with ``turn_in_flight`` still
        True and the exclusive ``"turn"`` worker still the running one --
        starting the rejection's own turn from there would cancel the
        worker that started it."""
        if not self.queued:
            return
        queued, self.queued = self.queued, []
        applied, refused = await asyncio.to_thread(
            diff_mod.flush, self.diff_cwd, queued
        )
        for item in refused:
            await self._tell_user(item.failure)
        for item in applied:
            await self._tell_agent(item.file_diff, item.hunk, item.reason)
        await self.refresh_diff()

    async def _tell_agent(
        self, file_diff: "diff_mod.FileDiff", hunk: "diff_mod.Hunk", reason: str
    ) -> None:
        """Send the rejection down the USER-AUTHORED path.

        ``SessionPane._run_turn`` is the same door a typed prompt goes
        through (``on_prompt_submitted``'s last line), and nothing on it
        touches :data:`doxa.peers.PEER_UNTRUSTED_INTRO`. That contrast is
        deliberate and it is the whole trust argument: a peer message is
        wrapped because ANOTHER AGENT wrote it; a human clicking reject
        in their own session is the user speaking, and framing it as
        untrusted data would tell the agent to weigh its own user's
        instruction as hearsay."""
        pane = self.session_pane()
        if pane is None:
            return
        text = diff_mod.reject_message(file_diff, hunk, reason)
        pane.run_worker(
            pane._run_turn(text), exclusive=True, group="turn"
        )

    async def _tell_user(self, text: str) -> None:
        pane = self.session_pane()
        if pane is not None:
            await pane._system(text)

    # -- wiring -------------------------------------------------------

    def session_pane(self) -> "Any | None":
        """The session this diff is of, or ``None`` once it is gone.

        Resolved by query rather than held as a reference: a pane can be
        closed, detached or restored underneath this one, and a stale
        widget reference is the defect class ``focused_pane``'s docstring
        already names -- a second answer to a question the DOM answers.

        Searched from the enclosing TAB, never from ``self.app``.
        ``Widget.app`` reads a context variable, so it is only reliable
        while the app's own message pump is on the stack -- and this runs
        from workers and, in the suite, straight from a test coroutine,
        where that variable can still name the PREVIOUS app. Measured:
        it made two tests pass alone and fail in file order, which is the
        worst way for a lookup to be wrong. Walking the DOM is also the
        more correct search -- the owner-first invariant puts a diff leaf
        in the same tab as the session it was opened from, so that tab is
        the whole search space by construction."""
        from ..session.pane import SessionPane

        node: Any = self.parent
        root: Any = None
        while node is not None:
            root = node
            if hasattr(node, "root_box") and hasattr(node, "leaves"):
                break  # the PaneTab
            node = node.parent
        if root is None:
            return None
        for pane in root.query(SessionPane):
            if getattr(pane, "_session_id", "") == self.session_id:
                return pane
        return None

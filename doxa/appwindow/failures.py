# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.appwindow.failures -- the error surface's app-level half.

Everything the window does once something has gone wrong: WHERE a failure
gets drawn, the single door every caller goes through to make one visible,
the form of that door for a caught exception, the traceback read that
names the widget which raised, the quarantine that stops a painting widget
from being painted again, Textual's own unhandled-exception funnel
overridden, and the exit that still says what happened on the way out.

v0.56.0 built this inside :class:`doxa.app.DoxaApp`, where it was one
contiguous block of seven methods under a ``-- the error surface`` banner.
It moves here unchanged -- same methods, same order, same words -- as the
first of DoxaApp's method families to become a mixin, which is what
:mod:`doxa.session` already did to SessionPane. Not one of the seven
carries a decorator, and that is why this family went first: a Textual
``@on`` handler declared on a plain mixin is never registered and never
fires, so a family that needs the metaclass has to stay in doxa/app.py.

:data:`RENDER_FRAMES` and :data:`FAILURE_ESCALATE` came along because the
methods that read them are their only readers. ``doxa.app`` imports both
back: they were module attributes there before this move, the suite reads
``doxa.app.FAILURE_ESCALATE``, and a refactor nothing downstream has to
know about is the point.
"""

from __future__ import annotations

import contextlib
from typing import Any

from textual.app import App
from textual.containers import VerticalScroll

from .. import errors as errors_mod
from .. import version as version_mod
from ..ui.transcript import ErrorBlock


# -- v0.56.0: the error surface's app-level half -----------------------
#
# Textual 5.3.0 funnels EVERYTHING through ``App._handle_exception``:
# message-handler raises, compose/mount raises, idle handlers, next-
# callbacks (textual/message_pump.py:585,647,669,682), a widget's own
# ``_compose`` (widget.py:4521), a failed worker with the default
# ``exit_on_error=True`` (worker.py:384, wrapped in ``WorkerFailed``) and
# the compositor's paint loop (app.py:3656). ``textual/_compositor.py``
# has no ``except`` in it at all, so there is NO per-widget render guard
# to hook -- the paint of a whole frame is what fails, and the one place
# that hears about it is that method. Its own docstring says "Always
# results in the app exiting", which is precisely the behaviour the four
# defects of 2026-08-24 arrived as.
#
# So DoxaApp overrides it. See :meth:`DoxaApp._handle_exception`.

#: Frames that mean "this raise happened while Textual was PAINTING".
#: Read off the traceback rather than tracked with a flag, because a flag
#: would have to be set and cleared on a path that runs every frame -- and
#: nothing in this app is allowed to cost anything per frame (see GitLine's
#: docstring and _refresh_status's note on the idle-CPU regression).
RENDER_FRAMES = frozenset({
    "render", "render_line", "render_lines", "_render_content", "render_str",
    "get_content_width", "get_content_height", "__rich_console__",
    "__rich_measure__", "render_map", "_arrange",
})

#: The same failure this many times is a failure that is never going to
#: stop -- a widget raising on every paint, which is the reported crash's
#: exact shape. Quarantine (see :meth:`DoxaApp._quarantine`) normally ends
#: it at the source on the FIRST one; this is the backstop for when it
#: cannot, and it escalates to a clean fatal exit with a report rather than
#: letting the app spin repainting a block about its own inability to
#: paint.
FAILURE_ESCALATE = 25

class WindowFailuresMixin:
    """DoxaApp's failure half. Mixed into the app, never used standalone:
    every method here reads window state through ``self`` -- the failure
    log, the blocks already on screen, the re-entry flag and the panes."""

    def _failure_surface(self) -> "VerticalScroll | None":
        """WHERE a failure gets drawn: the active session's transcript,
        falling back to any session's.

        The active tab is the right answer when there is one -- a failure
        belongs next to whatever the user was doing when it happened. The
        fallback exists because the active tab is often not a SessionPane
        at all (a subagent transcript, an archive) and because
        ``active_pane`` is None for the whole window before Textual
        has decided which tab is active (see :meth:`_activation_pending`),
        which is exactly when a boot-time failure fires. A failure with
        nowhere at all to go is not dropped -- see
        :meth:`report_failure`."""
        pane = self.active_pane or next(iter(self.panes()), None)
        if pane is None:
            return None
        try:
            return pane.query_one("#block-list", VerticalScroll)
        except Exception:  # noqa: BLE001 -- a pane mid-compose has no list yet
            return None

    def report_failure(self, failure: "errors_mod.Failure") -> None:
        """Make one failure VISIBLE. The single door; everything else in
        this file and every future caller goes through it.

        Order matters and is the opposite of the obvious one: the block is
        mounted FIRST and the log written second. Persisting first would
        let a read-only home directory or a full disk decide whether the
        user gets told, and the visible copy is the one that is not
        optional. :func:`doxa.errors.append` returning None is therefore a
        degraded bug report, never a hidden failure.

        Never swallows. Every path through this method ends with the
        failure recorded in :attr:`failures` and, if there is any surface
        at all, on screen; if there is not, it goes to the terminal
        (:meth:`_fail_fatally`) rather than nowhere. A caught exception
        that only reaches a log file is worse than the crash it replaced,
        because the tests still pass and the user still learns nothing.

        Repeats collapse. The block for a signature already on screen gets
        a tally, not a sibling -- and past :data:`FAILURE_ESCALATE` of them
        this stops being a recoverable failure by definition and exits with
        a report.

        **What a future plugin loader calls.** This, with an explicit
        ``origin`` of ``plugin:<name>`` (see
        :func:`doxa.errors.from_exception` and
        :func:`doxa.errors.policy_failure` -- the second is there for the
        ``text()`` time budget, which breaks a promise without raising).
        Reading ``app.failures.failed("plugin:jira")`` afterwards is the
        "disabled for the run" state the spec's failure policy needs. This
        release builds neither the loader nor the allowlist, deliberately;
        it builds the surface they would fail into."""
        if self._reporting:
            # Re-entered: the failure below happened WHILE drawing a block
            # about another one. Persist it (the log is the surface that
            # cannot itself fail into this method) and get out.
            errors_mod.append(failure)
            return
        self._reporting = True
        try:
            count = self.failures.record(failure)
            if failure.fatal or count > FAILURE_ESCALATE:
                self._fail_fatally(failure, repeats=count)
                return
            existing = self._error_blocks.get(failure.signature)
            # ``is_attached`` rather than ``is_mounted``: ``Widget.mount``
            # registers the child synchronously and only DISPATCHES its
            # Mount event a pump cycle later, and the repeat case is
            # exactly the one that fires several times inside one cycle (a
            # widget raising on every paint). Keying on is_mounted would
            # have grown one block per repeat until the pump caught up --
            # a column of identical blocks, which is what this is for.
            # Attachment is also the right question when the tab holding
            # the block has been closed: a detached block is gone from the
            # screen, so the next failure gets a fresh one.
            if existing is not None and existing.is_attached:
                # A repeat: tally the header and stop. Writing the same
                # scrubbed traceback to the log on every paint of a broken
                # widget would spend the whole rotation budget on one
                # failure and push out everything that came before it.
                existing.bump(count)
                return
            errors_mod.append(failure)
            block_list = self._failure_surface()
            if block_list is None:
                # Nothing on screen can hold it, and a failure nobody can
                # SEE is the defect this release exists to remove -- so it
                # goes to the terminal instead of nowhere.
                self._fail_fatally(failure, repeats=count, logged=True)
                return
            block = ErrorBlock(failure)
            self._error_blocks[failure.signature] = block
            block_list.mount(block)
            block_list.scroll_end(animate=False)
        finally:
            self._reporting = False

    def report_exception(
        self,
        error: BaseException,
        *,
        origin: "str | None" = None,
        context: str = "",
        fatal: bool = False,
    ) -> None:
        """:meth:`report_failure` for a caught exception -- the form every
        ``except`` block in this codebase that wants to STOP swallowing
        should reach for. Scrubbing and attribution happen in
        :func:`doxa.errors.from_exception`; nothing raw reaches a
        widget."""
        self.report_failure(
            errors_mod.from_exception(
                error, origin=origin, context=context, fatal=fatal,
            )
        )

    def _culprit_widget(self, error: BaseException) -> "tuple[Any, bool]":
        """``(widget, was_painting)`` read off the traceback.

        ``was_painting`` is true when any frame in the stack is one of
        :data:`RENDER_FRAMES` or lives in Textual's compositor -- the
        reported crash's shape (``textual_image`` querying stdin from
        inside ``__rich_console__`` while Textual owned the terminal).
        ``widget`` is the DEEPEST frame whose ``self`` is a mounted Widget
        that is neither this app nor a Screen: for that crash it is the
        image widget itself, which is the thing that has to stop being
        asked to paint.

        Never raises: a frame that cannot be read is skipped rather than
        blamed, because this runs inside the handler of last resort."""
        from textual.screen import Screen
        from textual.widget import Widget

        tb = getattr(error, "__traceback__", None)
        frames = []
        while tb is not None:
            frames.append(tb.tb_frame)
            tb = tb.tb_next
        painting = False
        culprit: "Any" = None
        for frame in reversed(frames):
            try:
                name = frame.f_code.co_name
                filename = str(frame.f_code.co_filename)
                candidate = frame.f_locals.get("self")
            except Exception:  # noqa: BLE001 -- unreadable frame, not a suspect
                continue
            if name in RENDER_FRAMES or filename.endswith("_compositor.py"):
                painting = True
            if (
                culprit is None
                and isinstance(candidate, Widget)
                and not isinstance(candidate, (App, Screen))
                and candidate.is_mounted
            ):
                culprit = candidate
        return culprit, painting

    def _quarantine(self, error: BaseException) -> str:
        """Stop a painting widget from being painted again, and say what
        was done. Returns the context line for the block.

        This is the containment Textual does not offer. ``_compositor.py``
        has no exception handling whatsoever, so a widget that raises while
        rendering does not fail alone -- it takes the whole FRAME with it,
        every frame, forever. Merely surviving the raise would leave the
        app alive and unable to draw, spinning on a failure it re-hits on
        the next repaint; the tally in :meth:`report_failure` would count
        to :data:`FAILURE_ESCALATE` and give up.

        So the widget is hidden. ``display = False`` takes it out of the
        layout entirely, which is the one thing that ends the loop at its
        source, and the error block that replaces it says so -- half a
        widget silently missing is one of the four defects this release is
        about, and a whole widget silently missing would be the same
        defect wearing a fix.

        Only for a RENDER failure, and only for a widget we can actually
        name. A message-handler raise gets no quarantine: hiding an
        arbitrary widget because a keystroke handler threw would be a
        second defect, not containment.

        DOXA owns the general containment here. The specific cause of the
        reported crash -- textual-image probing stdin for the terminal's
        cell size during a paint -- is fixed where it belongs, in
        :mod:`doxa.images`/:mod:`doxa.banner` (the ``fix/banner-not-
        rendering`` work), and the two do not overlap: that one stops the
        probe happening, this one stops ANY render raise being fatal."""
        culprit, painting = self._culprit_widget(error)
        if not painting:
            return "handling an event"
        if culprit is None:
            return "painting the screen"
        name = type(culprit).__name__
        with contextlib.suppress(Exception):
            culprit.display = False
            return f"painting {name} — hidden so the rest of DOXA keeps working"
        return f"painting {name}"

    def _handle_exception(self, error: Exception) -> None:
        """Textual's one funnel for everything unhandled, overridden.

        In Textual 5.3.0 this method receives message-handler raises,
        compose/mount raises, idle and next-callback raises, failed workers
        (as ``WorkerFailed``, because ``run_worker``'s ``exit_on_error``
        defaults to True) and the compositor's own paint failures -- the
        file:line evidence is in the module-level note beside
        :data:`RENDER_FRAMES`. Its stock behaviour is documented as
        "Always results in the app exiting", and every one of the four
        defects of 2026-08-24 that did not simply vanish arrived that way:
        as a bare traceback on a terminal whose TUI had gone.

        Worker failures are the case worth stating separately, because the
        brief for this release asked whether a dying worker reaches
        anything today. It does -- it reaches HERE, and here used to mean
        the app exits. DOXA starts a worker for nearly everything
        (``_boot``, ``_peer_pump``, every slash command, ``_derive_once``,
        the update check), so "a worker died" was indistinguishable from
        "DOXA crashed", and a worker cancelled at teardown is a routine
        event. Now a failed worker is a visible block and the session it
        belonged to stays usable.

        A deliberate exit is not a defect and never arrives here:
        ``KeyboardInterrupt`` and ``SystemExit`` derive from
        BaseException, not Exception, so Textual's own ``except Exception``
        clauses do not catch them and this signature cannot receive them.
        (Ctrl+C itself is not bound to anything in this file as of
        v0.85.0 -- see the BINDINGS comment on DoxaApp -- so it no longer
        even reaches this question in the ordinary case; the guard below
        is belt-and-braces for whatever path DOES still raise one, a
        pre-app-start KeyboardInterrupt included, and hands it straight
        back to Textual/Python rather than swallowing it.)

        Fatal is still possible and still SEEN: a failure with nowhere to
        draw itself, or one that will not stop repeating, exits through
        :meth:`_fail_fatally`, which prints the same information to the
        terminal on the way out."""
        if not isinstance(error, Exception):  # pragma: no cover -- see docstring
            super()._handle_exception(error)
            return
        from textual.worker import WorkerFailed

        if isinstance(error, WorkerFailed):
            # Unwrap: WorkerFailed is Textual's envelope, and the
            # traceback that matters (and the attribution read off it) is
            # the worker body's own.
            inner = getattr(error, "error", None)
            if isinstance(inner, BaseException):
                error = inner  # type: ignore[assignment]
            context = "running a background task"
        else:
            context = self._quarantine(error)
        self.report_failure(
            # No explicit origin: nothing routed HERE knows whose code it
            # is, and errors.origin_of reads it off the traceback. An
            # explicit origin is what a CALLER passes -- a plugin loader
            # that knows it just called into plugin:jira, or the
            # needs-input path in doxa.session.runtime that knows what it
            # was doing.
            errors_mod.from_exception(error, context=context)
        )

    def _fail_fatally(
        self, failure: "errors_mod.Failure", repeats: int = 1, logged: bool = False,
    ) -> None:
        """DOXA cannot carry on -- so say the same thing on the way out.

        A user who has to file a bug report should not have to reconstruct
        which DOXA, which terminal and which operation from a bare Python
        traceback. ``/about`` already assembles exactly that block
        (:func:`doxa.version.about_text` -- version, sha, interpreter,
        textual, agent SDK, lore and where it was loaded from, platform,
        keyboard protocol, config path), it is the block the about dialog's
        copy door puts on the clipboard, and reusing it here means the
        crash report and the thing the user would have pasted are the same
        text rather than two divergent almost-truths.

        Scrubbed, like everything else that leaves this process: the
        traceback in ``failure.detail`` went through
        :func:`doxa.errors.scrub` at construction, and the about block is
        DOXA's own measurements rather than anything model- or
        environment-derived. Notably this replaces Textual's own
        ``_fatal_error``, which renders
        ``rich.traceback.Traceback(show_locals=True)`` -- the frame locals
        are where a credential actually lives, and printing them into a
        terminal a user is about to screenshot is the leak this release
        must not ship.

        ``self._exception`` is set so that a test harness still learns the
        app died (``App.run_test`` re-raises it at shutdown) -- a fatal
        failure that a suite could pass straight through would make this
        module a place errors hide, which is exactly what it is for."""
        if not logged:
            errors_mod.append(failure)
        tally = f"  (×{repeats})" if repeats > 1 else ""
        report = "\n".join((
            f"DOXA stopped: {failure.headline()}{tally}",
            "",
            version_mod.about_text(self.update_available),
            "",
            failure.detail or "(no further detail)",
            "",
            f"This was also written to {errors_mod.log_path()}",
        ))
        self._return_code = 1
        if self._exception is None:
            # The same two lines Textual's own _handle_exception writes, so
            # App.run_test's teardown re-raise and Pilot's exception wait
            # both behave exactly as they would have without this override.
            self._exception = errors_mod.FatalFailure(failure.headline())
            self._exception_event.set()
        with contextlib.suppress(Exception):
            from rich.text import Text

            self.panic(Text(report))

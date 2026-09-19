# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.ui.fleettab -- a fleet run, watched in a tab.

A read-only ``TabPane`` beside the session tabs, exactly the shape
:class:`doxa.ui.transcript.ArchivedSessionTab` and
:class:`doxa.ui.transcript.SubagentTranscriptTab` already are and for the
same reason: it is not a session. There is no engine, no prompt and no
turn, so a ``SessionPane`` with a prompt box that refused every prompt
would be a worse answer than a pane that visibly has none.

IT READS FILES, NEVER THE RUN. The whole content comes from
:func:`doxa.fleetview.render` over a :class:`doxa.fleetview.RunSnapshot`,
which is two file reads -- the run's manifest and the run's ledger. The
:class:`doxa.fleetsession.FleetSession` this tab is handed is consulted
for exactly three things a file cannot say: whether the run's task is
still going, whether ``/fleet detach`` was asked for, and the refusal
that happened before any manifest existed. Nothing here touches a
``FleetRun``, its slots or its backend, because those are mutated by a
coroutine this widget's timer knows nothing about.

THE TEXT IS RENDERED AS TEXT. The ledger tail carries message bodies
written by agents, so the panel updates through a ``rich.text.Text``
rather than a markup string: a body containing ``[bold]`` is then
characters at every point in its life, with no escaping call anybody has
to remember at a new call site. Same structural posture
``doxa.meshgraph`` takes for the same bodies in a browser.
"""

from __future__ import annotations

import contextlib
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static, TabPane

from .. import fleetview as fleetview_mod

__all__ = ["REFRESH_SECS", "FleetTab"]

#: How often a LIVE run's tab re-reads its two files. Half a second: a
#: phase change (spawned -> armed -> dispatched) is the thing a watcher
#: is waiting for and anything slower reads as a freeze, while the cost
#: is two reads of a few KB. A finished run's timer is stopped
#: altogether -- see :meth:`FleetTab._refresh`.
REFRESH_SECS = 0.5


class FleetTab(TabPane):
    """One run's tab: ``fleet <run-id>``, read-only, refreshed on a timer.

    Titled with the run id rather than the prompt, because the run id is
    what ``/fleet runs``, the manifest path and the mesh URL all name, and
    a tab whose title matches none of those is a tab the operator cannot
    connect to anything."""

    def __init__(
        self,
        session: Any,
        *,
        owner: Any = None,
        id: "str | None" = None,
    ) -> None:
        self.session = session
        #: The ``SessionPane`` whose ``/fleet start`` opened this, the same
        #: back-reference :class:`doxa.ui.transcript.SubagentTranscriptTab`
        #: carries and for a sharper reason: this tab is not a session, so
        #: ``DoxaApp.active_pane`` is None while it is the active tab --
        #: and a palette entry that ran a slash command would then reach
        #: nobody. It is how "Fleet: status" still works from the tab the
        #: run is IN.
        self.owner = owner
        self.run_id = str(getattr(session, "run_id", "") or "?")
        self.base_label = f"fleet {self.run_id}"
        self.body = Static("", classes="fleet-body")
        self.scroll = VerticalScroll(self.body, classes="fleet-scroll")
        self._timer: Any = None
        super().__init__(self.base_label, id=id)

    def compose(self) -> ComposeResult:
        yield self.scroll

    def on_mount(self) -> None:
        self._refresh()
        self._timer = self.set_interval(REFRESH_SECS, self._refresh)

    def on_unmount(self) -> None:
        self._cancel_timer()

    # -- the one read --------------------------------------------------

    def text(self) -> str:
        """What this tab currently says, as plain text.

        Public because it is what a test asserts on and what a future
        "copy this report" affordance would take -- the panel's rendered
        Text is a view of this string, never a second source."""
        session = self.session
        mesh_url = ""
        app = self._safe_app()
        if app is not None:
            with contextlib.suppress(Exception):
                mesh_url = str(
                    app.mesh_url_for(session.ledger_path) or ""
                )
        return fleetview_mod.render(
            session.snapshot(),
            mesh_url=mesh_url,
            detached=bool(getattr(session, "detached", False)),
            note=str(getattr(session, "note", "") or ""),
        )

    def _refresh(self) -> None:
        """Repaint, and stop the timer once there is nothing left to
        change.

        A finished run's files do not move again, so a tab that kept
        polling one would be a timer per dead run for as long as the tab
        stays open -- and the tab is meant to stay open, holding the final
        report. One last paint AFTER the task ends (hence the read of
        ``alive`` before the paint, not after) is what makes the final
        state the one on screen."""
        alive = bool(getattr(self.session, "alive", False))
        with contextlib.suppress(Exception):
            self.body.update(Text(self.text()))
        if not alive:
            self._cancel_timer()

    def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            with contextlib.suppress(Exception):
                timer.stop()

    def _safe_app(self) -> Any:
        """``self.app`` without the raise.

        ``Widget.app`` raises when the widget is not mounted under a
        running app, which is exactly the state a renderer test builds the
        tab in. The mesh URL is decoration on this panel; the run's own
        state is not, and one must not cost the other."""
        try:
            return self.app
        except Exception:  # noqa: BLE001 -- see the docstring
            return None

# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.fleetsession -- one fleet run, owned by one TUI session.

``doxa.fleet`` is a HARNESS: it runs N sessions to quiescence and returns
a report, which is exactly what ``doxa-fleet`` wants and exactly what a
terminal application cannot call. This module is the difference -- the
run as something that starts, is watched, is asked to stop, and is
answered about, from a ``/fleet`` command typed in a session that is
itself still working.

THREE THINGS IT OWNS, and nothing else:

1. **The task.** ``FleetRun.run()`` on the TUI's own event loop, never a
   thread and never a subprocess. The run's sessions are separate
   processes already (that is what the harness does); the ORCHESTRATION
   is a few awaits and a poll loop, and putting it on the loop that is
   already running is what lets ``/fleet stop`` be a method call rather
   than a signal.
2. **The stop.** :meth:`request_stop` sets the run's own stop event
   (:meth:`doxa.fleet.FleetRun.request_stop`), which ends it at the next
   phase boundary and drops it into the SAME teardown the quiescence
   deadline drops it into. Never ``task.cancel()``: teardown escalates
   stop -> SIGTERM -> SIGKILL and then asks the OS whether the process
   really went, and a cancellation thrown into the middle of that is how
   a run leaves daemons behind.
3. **The manifest heartbeat.** The run writes its manifest at the end;
   a watcher needs it during. :meth:`_heartbeat` calls
   ``write_manifest()`` on a timer while the run is live, so the fleet
   tab can read the run from two files and never touch a run object.

A RUN IS NOT A BACKGROUND SERVICE. :attr:`detached` starts False, and the
tab's close handler asks for a stop unless it has been set (``/fleet
detach``). The alternative -- a TUI that can leave thirty-two daemons and
an unbounded spend behind by closing a tab -- is the failure
:func:`doxa.fleet.check_run_budget` already refuses to let an operator
reach by forgetting a flag, and it must not be reachable by forgetting a
tab either.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from . import fleetview as fleetview_mod

__all__ = ["FleetSession", "HEARTBEAT_SECS"]

#: How often the run rewrites its own manifest while it is live. One
#: second: the manifest is a few KB of JSON for N<=32 and the tab's own
#: refresh is on the same order, so anything faster buys a reader nothing
#: and anything slower makes a phase change look like a freeze.
HEARTBEAT_SECS = 1.0


class FleetSession:
    """One run, from the session that started it.

    Constructed with a built :class:`doxa.fleet.FleetSpec` -- the spec is
    the command's job (it comes from ``doxa.fleet.build_parser``, which is
    the same grammar ``doxa-fleet`` parses), so that this class has one
    responsibility and a test can drive it with any spec and any
    backend."""

    def __init__(
        self,
        spec: Any,
        *,
        backend: Any = None,
        force: bool = False,
    ) -> None:
        self.spec = spec
        self.backend = backend
        self.force = bool(force)
        self.run: Any = None
        self.report: Any = None
        self.task: "asyncio.Task | None" = None
        #: A refusal that happened before the run existed -- the capacity
        #: arithmetic, the missing run budget, a run root too deep for a
        #: Unix socket. The tab's FIRST LINE, never a traceback in a
        #: worker nobody is reading.
        self.note: str = ""
        #: ``/fleet detach`` was asked for: closing the tab now leaves the
        #: run going. False by default, deliberately.
        self.detached: bool = False
        self._stop = asyncio.Event()

    # -- what it is ----------------------------------------------------

    @property
    def run_id(self) -> str:
        return str(getattr(self.spec, "run_id", "") or "")

    @property
    def run_root(self) -> Path:
        return Path(self.spec.run_root)

    @property
    def ledger_path(self) -> Path:
        return Path(self.spec.ledger_path)

    @property
    def alive(self) -> bool:
        """The run's task is still going. False before :meth:`start` and
        once the report (or the refusal) has landed."""
        return self.task is not None and not self.task.done()

    def snapshot(self, limit: int = fleetview_mod.LEDGER_TAIL) -> "fleetview_mod.RunSnapshot":
        """This run as the two files say it is, right now. The ONE way
        anything outside this object reads a run's state."""
        return fleetview_mod.RunSnapshot.read(self.run_root, limit)

    # -- the lifecycle -------------------------------------------------

    def start(self) -> "asyncio.Task":
        """Put the run on the current event loop. Idempotent: a second
        call returns the task the first one made."""
        if self.task is None:
            self.task = asyncio.create_task(
                self._drive(), name=f"fleet-{self.run_id}"
            )
        return self.task

    def request_stop(self) -> None:
        """End the run and tear down, now.

        Reaches the run through its own stop event, so a run that has not
        started yet (the task exists, ``prepare`` has not returned) is
        still stopped: :class:`doxa.fleet.FleetRun` reads the event at
        every phase boundary."""
        self._stop.set()
        run = self.run
        if run is not None:
            run.request_stop()

    def detach(self) -> None:
        self.detached = True

    async def _drive(self) -> None:
        """The run, plus the heartbeat that makes it readable while it
        happens.

        Every refusal is CAUGHT and kept as :attr:`note` rather than
        raised: this coroutine is a task nobody awaits, so an exception
        here would be a "Task exception was never retrieved" on stderr
        behind a full-screen terminal app -- which is the same as no
        message at all."""
        from . import fleet as fleet_mod

        run = fleet_mod.FleetRun(
            self.spec, self.backend, force=self.force, stop=self._stop
        )
        self.run = run
        heartbeat = asyncio.create_task(self._heartbeat(run))
        try:
            self.report = await run.run()
        except (fleet_mod.CapacityRefused, fleet_mod.BudgetRefused) as exc:
            self.note = str(exc)
        except ValueError as exc:
            # check_socket_budget's own refusal, which names the byte
            # arithmetic and the fix (a shorter --root). It is a ValueError
            # rather than a dedicated type in doxa.fleet and is caught by
            # type here rather than by message.
            self.note = str(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- see the docstring
            self.note = f"the run failed: {type(exc).__name__}: {exc}"
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat
            self.report = self.report or getattr(run, "report", None)

    async def _heartbeat(self, run: Any) -> None:
        """Rewrite the manifest while the run is live.

        The run writes it once, at the end, in the ``finally`` that
        guarantees a record exists even for a run that threw. That is the
        right contract for a harness and useless to a watcher, so this
        adds the only thing a watcher needs: the same file, more often.
        Best-effort by construction -- a failed write costs one refresh,
        never the run."""
        while True:
            await asyncio.sleep(HEARTBEAT_SECS)
            if not getattr(run.report, "live", False):
                continue
            with contextlib.suppress(Exception):
                run.write_manifest()

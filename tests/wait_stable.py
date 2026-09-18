# SPDX-License-Identifier: AGPL-3.0-only
"""Wait for a condition to SETTLE, not merely to touch true once.

Four tests -- tests/test_errors.py::test_quarantine_says_what_it_hid,
tests/test_memory_chip_writes.py::test_approving_a_memory_proposal_moves_
memory_and_staged_chips, tests/test_picker_row_actions.py::test_the_in_
flight_marker_clears_once_the_debounce_settles and tests/test_diff_chip.py
::test_the_pane_paints_the_wash_at_its_real_width -- all passed in
isolation and failed only under full-suite load, and all four had the same
shape: poll for a condition, stop at the FIRST poll that reads true, then
immediately assert something that assumes the state is not just true but
SETTLED. A wait built that way is only as safe as "first true" happening
to already mean "done", which async paint/layout does not promise --
Textual can report a provisional value (a widget's size mid-layout, a
debounce marker cleared a frame ahead of the rebuild it announces) on its
way to the real one. Cheap under a lightly-loaded suite, where the two
line up often enough that the gap never gets exercised; not cheap once a
full run is competing for the event loop and the frames stretch out
enough to land an assertion inside that gap.

The fix is not a longer sleep -- a slower flake is still a flake, and the
four above already tried timeout budgets from 100 to 300 tries without
becoming reliable. It is to require the SAME true reading several polls
in a row before trusting it, exactly as e.g. TCP requires more than one
ACK before it believes a connection, or a debounced UI input requires the
value to stop changing before it acts. :func:`wait_stable` (and the
lower-level :func:`wait_stable_ticking` it is built on) do that: they
return only once ``predicate()`` has read true on ``stable_frames``
CONSECUTIVE checks, resetting the count on every false in between -- so a
provisional true that reverts is caught (the count resets and waiting
resumes) rather than being the value the caller acts on.

A caller still writes its own ``assert predicate()`` (or a richer
assertion over the same values) right after the wait returns, same as the
uses below -- the wait is what makes that assertion land on settled
state, not a replacement for having one. Waiting on the assertion's own
compound condition instead of a plain, single-purpose predicate would
make the test vacuous: a helper that only ever reports "it became true
eventually" and never lets a genuinely-broken invariant fail loudly.

:func:`wait_stable` drives the wait with ``pilot.pause()`` -- the right
choice for nearly every caller, and how the other three tests above use
it. tests/test_errors.py is the one exception: its own ``_paint`` helper
documents, at length, that ``Pilot.pause`` calls
``Screen._on_timer_update`` OUTSIDE DoxaApp's guarded exception path,
where a render raise reaches the test directly instead of being contained
into an ``ErrorBlock`` -- which is the one thing that test is proving
does NOT happen. Confirming that containment STAYS true a few frames on
cannot poll through the one door documented to bypass the containment
itself, so :func:`wait_stable_ticking` takes the advance-one-frame step as
a plain argument, and that test drives it with ``asyncio.sleep`` instead
-- still through the guarded path (Textual's own screen-update timer),
never through ``Pilot.pause``. A fifth caller with the same constraint has
the identical escape hatch; everyone else wants :func:`wait_stable`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

#: The interval every polling helper already in this suite converged on
#: independently (tests/helpers.py's ``_picker``/``_pending_picker``,
#: test_diff_chip.py's own ``_wait``, test_memory_chip_writes.py's own
#: ``_wait``/``_wait_status``) -- long enough to let one debounce timer or
#: message-pump cycle land, short enough that a generous try-count still
#: bails out in a fraction of a second when a predicate is simply wrong
#: rather than merely late.
DEFAULT_INTERVAL = 0.02

#: Generous rather than tight: stability costs a HANDFUL of extra polls
#: (``stable_frames`` of them, worst case, on top of however long the
#: state took to first turn true) and the four callers this was written
#: for were already budgeting 100-300 tries for "first true" alone.
DEFAULT_TRIES = 200

#: Enough consecutive true reads that a provisional value which reverts
#: after one or two frames cannot pass for settled, without demanding so
#: many that a slow-but-genuine settle (the exact thing full-suite load
#: does to every one of these tests) reads as never-stabilizing.
DEFAULT_STABLE_FRAMES = 5


async def wait_stable_ticking(
    tick: "Callable[[], Awaitable[object]]",
    predicate: "Callable[[], bool]",
    *,
    tries: int = DEFAULT_TRIES,
    stable_frames: int = DEFAULT_STABLE_FRAMES,
) -> None:
    """Wait for ``predicate()`` to read true on ``stable_frames``
    consecutive checks, calling and awaiting ``tick()`` between checks to
    give the state a chance to move. Raises ``AssertionError`` -- never
    returns false -- if it runs out of ``tries`` first, so a caller's own
    follow-up ``assert predicate()`` is confirming settled state, not
    gambling on it.

    Low-level: :func:`wait_stable` is what nearly every caller wants.
    This exists for the one case (see the module docstring) where driving
    frames with ``pilot.pause()`` would poll through a door the test
    itself must NOT go through -- there, the caller supplies its own
    ``tick``."""
    consecutive = 0
    for _ in range(tries):
        if predicate():
            consecutive += 1
            if consecutive >= stable_frames:
                return
        else:
            consecutive = 0
        await tick()
    raise AssertionError(
        f"condition never held {stable_frames} consecutive frames "
        f"(reached {consecutive} in a row over {tries} tries)"
    )


async def wait_stable(
    pilot,
    predicate: "Callable[[], bool]",
    *,
    tries: int = DEFAULT_TRIES,
    stable_frames: int = DEFAULT_STABLE_FRAMES,
    interval: float = DEFAULT_INTERVAL,
) -> None:
    """:func:`wait_stable_ticking`, advancing one frame at a time with
    ``pilot.pause(interval)`` -- the ordinary case, and every one of these
    tests except tests/test_errors.py's ``test_quarantine_says_what_it_
    hid`` (see the module docstring for why that one is different)."""
    await wait_stable_ticking(
        lambda: pilot.pause(interval),
        predicate,
        tries=tries,
        stable_frames=stable_frames,
    )

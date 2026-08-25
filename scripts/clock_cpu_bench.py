# SPDX-License-Identifier: AGPL-3.0-only
"""Measure the upper-right clock's real idle-CPU cost (item M).

Three headless runs, each its own process (so a shared warm cache in one
mode can't bleed into another's number): the clock off, on with seconds
hidden (minute-aligned re-arm), on with seconds shown (second-aligned
re-arm). Each boots a real DoxaApp under Textual's `run_test` harness (a
scripted FakeEngine underneath -- no SDK call, no spend), lets it settle,
then idles for an 8-second window while sampling this PROCESS's own
`/proc/self/stat` utime+stime before and after -- the same measurement
doxa/app.py's own idle-CPU regression note (ThinkingMarker's docstring)
used to characterize the leaked-timer bug this app already paid for once.

    uv run python scripts/clock_cpu_bench.py            # all three modes
    uv run python scripts/clock_cpu_bench.py --mode off  # one, for CI-ish use

%CPU = 100 * (utime+stime delta in seconds) / wall-clock seconds elapsed,
over the 8s idle window only -- app construction and the Textual boot
sequence are excluded from the window on purpose, since this is a
statement about what the clock costs at rest, not about startup.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WINDOW_SECS = 8.0
_CLK_TCK = os.sysconf("SC_CLK_TCK")

MODES: dict[str, dict[str, str]] = {
    "off": {"DOXA_CLOCK_SHOW": "0"},
    "on-no-seconds": {"DOXA_CLOCK_SHOW": "1", "DOXA_CLOCK_SECONDS": "0"},
    "on-seconds": {"DOXA_CLOCK_SHOW": "1", "DOXA_CLOCK_SECONDS": "1"},
}


def _cpu_ticks() -> int:
    """utime + stime, in clock ticks, for THIS process -- fields 14/15 of
    /proc/self/stat (1-indexed; the field split is robust to a comm name
    containing spaces or parens because it splits after the ')' that
    closes the second, parenthesized field)."""
    raw = Path("/proc/self/stat").read_text()
    after_comm = raw[raw.rfind(")") + 2:]
    fields = after_comm.split()
    utime, stime = int(fields[11]), int(fields[12])  # 0-indexed from field 3
    return utime + stime


async def _run_one(mode: str) -> float:
    for key in ("DOXA_CLOCK_SHOW", "DOXA_CLOCK_SECONDS"):
        os.environ.pop(key, None)
    os.environ.update(MODES[mode])

    from doxa.app import DoxaApp
    from doxa import config as config_mod
    from tests.fakes import FakeEngine

    config_mod.invalidate()
    app = DoxaApp(cwd=str(ROOT), engine_factory=lambda: FakeEngine([]))
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.5)  # let boot settle out of the window

        start_ticks = _cpu_ticks()
        start_wall = time.monotonic()
        await asyncio.sleep(WINDOW_SECS)
        elapsed = time.monotonic() - start_wall
        delta_ticks = _cpu_ticks() - start_ticks

    cpu_secs = delta_ticks / _CLK_TCK
    return 100.0 * cpu_secs / elapsed


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=sorted(MODES), default=None)
    args = parser.parse_args(argv)
    if args.mode is not None:
        # The leaf invocation: measure exactly one mode, in THIS process.
        pct = asyncio.run(_run_one(args.mode))
        print(f"{args.mode:<16} {pct:>21.2f}%")
        return
    # The driver invocation: one child process PER mode, so a warm import
    # cache or a leftover env var from one measurement can never bleed
    # into the next one's number.
    import subprocess

    print(f"{'mode':<16} {'%CPU over ' + str(WINDOW_SECS) + 's idle':>22}")
    sys.stdout.flush()
    for mode in MODES:
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--mode", mode],
            check=True, cwd=str(ROOT),
        )


if __name__ == "__main__":
    main(sys.argv[1:])

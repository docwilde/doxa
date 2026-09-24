#!/usr/bin/env python3
"""Headless DOXA UI benchmark; no SDK, daemon, terminal, or network required.

The interaction samples include a state change, queued message processing,
and a forced compositor update (Pilot.pause(0)). The zero delay avoids
Textual's CPU-idle heuristic, which can add up to one second per sample.
Run this script with the same revision and interpreter when comparing
Textual versions or Python packaging modes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

from textual.containers import VerticalScroll

from doxa import config as config_mod, layout
from doxa.app import DoxaApp
from doxa.ui.transcript import TurnBlock
from tests.fakes import FakeEngine


SIZE = (160, 48)
WIDTHS = tuple(range(22, 42))
CHUNKS = tuple(f"delta {i:03d}: " + ("x" * 28) + "\n" for i in range(100))
SEED = "".join(f"- transcript line {i:03d}: reproducible text\n" for i in range(120))


def stats(samples_ns: list[int]) -> dict[str, float | int]:
    values = sorted(value / 1_000_000 for value in samples_ns)
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50_ms": round(statistics.median(values), 4),
        "p95_ms": round(values[min(len(values) - 1, int(len(values) * 0.95))], 4),
        "min_ms": round(values[0], 4),
        "max_ms": round(values[-1], 4),
    }


def make_app(cwd: str) -> DoxaApp:
    serial = 0

    def engine() -> FakeEngine:
        nonlocal serial
        serial += 1
        fake = FakeEngine([], cwd=cwd)
        fake.session_id = f"bench-{serial}"
        return fake

    return DoxaApp(cwd=cwd, engine_factory=engine, new_session_factory=engine)


async def app_startup(cwd: str) -> int:
    config_mod.invalidate()
    started = time.perf_counter_ns()
    app = make_app(cwd)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0)
        if len(app.panes()) != 1 or app.panes()[0].region.width < 1:
            raise RuntimeError("first UI frame did not paint")
        elapsed = time.perf_counter_ns() - started
    return elapsed


async def interactions(cwd: str) -> dict[str, list[int]]:
    config_mod.invalidate()
    app = make_app(cwd)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        # Four tabs in the left group, three in the right group.
        for _ in range(3):
            await app.action_new_tab()
        await pilot.pause()
        note = await app.split_active_pane(layout.ROW)
        if note:
            raise RuntimeError(f"split refused: {note}")
        await pilot.pause()
        for _ in range(2):
            await app.action_new_tab()
        await pilot.pause()
        if len(app.panes()) != 7 or len(app.groups()) != 2:
            raise RuntimeError("fixture needs seven sessions in two groups")
        if app.set_sidebar(True):
            raise RuntimeError("sidebar refused at 160x48")
        await pilot.pause()
        print("fixture ready", file=sys.stderr, flush=True)

        resize_ns: list[int] = []
        for width in WIDTHS:
            started = time.perf_counter_ns()
            note = app.resize_sidebar(width, persist=False)
            if note:
                raise RuntimeError(f"sidebar resize refused: {note}")
            await pilot.pause(0)
            resize_ns.append(time.perf_counter_ns() - started)
        print("resize done", file=sys.stderr, flush=True)

        # One visible turn in each group; the other five sessions remain
        # mounted as tabs and contribute the same widget/layout overhead.
        visible = []
        for group in app.groups():
            pane = group.tabbed.active_pane.focused_leaf
            if pane is None:
                raise RuntimeError("group has no visible session")
            block_list = pane.query_one("#block-list", VerticalScroll)
            turn = TurnBlock("Benchmark transcript")
            await block_list.mount(turn)
            await turn.append_text(SEED)
            visible.append((block_list, turn))
        await pilot.pause()
        print("transcripts ready", file=sys.stderr, flush=True)

        block_list, turn = visible[-1]
        append_ns: list[int] = []
        expected_source_len = len(turn.body.source)
        for chunk in CHUNKS:
            started = time.perf_counter_ns()
            await turn.append_text(chunk)
            block_list.scroll_end(animate=False)
            expected_source_len += len(chunk)
            for _ in range(12):
                await pilot.pause(0)
                if len(turn.body.source) >= expected_source_len:
                    # The stream runs in a background task. One further
                    # barrier paints blocks mounted by Markdown.append.
                    await pilot.pause(0)
                    break
            else:
                raise RuntimeError("streamed chunk did not reach Markdown")
            append_ns.append(time.perf_counter_ns() - started)
        print("append done", file=sys.stderr, flush=True)
        await turn.mark_done(None, None, False)
        await pilot.pause()

        scroll_ns: list[int] = []
        block_list.scroll_home(animate=False)
        await pilot.pause()
        for _ in range(100):
            started = time.perf_counter_ns()
            block_list.scroll_down(animate=False)
            await pilot.pause(0)
            scroll_ns.append(time.perf_counter_ns() - started)
        print("scroll done", file=sys.stderr, flush=True)
        return {"resize": resize_ns, "append": append_ns, "scroll": scroll_ns}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--startup-runs", type=int, default=10)
    parser.add_argument("--interaction-runs", type=int, default=3)
    args = parser.parse_args()
    if args.startup_runs < 0 or args.interaction_runs < 0:
        parser.error("run counts must be nonnegative")

    import textual

    # A source checkout otherwise starts a real background git fetch on
    # every App mount, while frozen bundles do not. A fresh temporary home
    # also triggers the first-run wizard once. Neither belongs in a drawing
    # comparison, so make these test-only switches unconditional here.
    os.environ["DOXA_SKIP_UPDATE_CHECK"] = "1"
    os.environ["DOXA_SKIP_FIRST_RUN"] = "1"

    samples: dict[str, list[int]] = {
        name: [] for name in ("startup", "resize", "append", "scroll")
    }
    with tempfile.TemporaryDirectory(prefix="doxa-ui-bench-") as home:
        os.environ["DOXA_HOME"] = str(Path(home) / "doxa-home")
        os.environ["DOXA_RUNTIME_DIR"] = str(Path(home) / "runtime")
        for i in range(args.startup_runs):
            samples["startup"].append(await app_startup(home))
            print(f"startup {i + 1}/{args.startup_runs}", file=sys.stderr, flush=True)
        for i in range(args.interaction_runs):
            result = await interactions(home)
            for name, values in result.items():
                samples[name].extend(values)
            print(
                f"interaction {i + 1}/{args.interaction_runs}",
                file=sys.stderr,
                flush=True,
            )
    print(
        json.dumps(
            {
                "source": "DOXA real headless UI + FakeEngine",
                "python": platform.python_version(),
                "textual": textual.__version__,
                "size": SIZE,
                "tabs": 7,
                "groups": 2,
                "sidebar_widths": [WIDTHS[0], WIDTHS[-1]],
                "results": {name: stats(values) for name, values in samples.items()},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())

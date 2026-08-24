"""Render the README's screenshot gallery -- no live SDK, no spend.

Drives DoxaApp headlessly, one Textual pilot session per SCENE, each scene
scripting a FakeEngine (and, where a scene needs live-registry state the
app would otherwise read from disk, patching doxa.peers for the duration)
to put a real feature on screen: the shell mid-conversation, the trace
tree, the settings modal, /sessions, and a belief-search tool call opened
up. Saves each as assets/shots/<scene>.svg; convert to PNG with inkscape
(CI does not need to -- the SVGs are committed too):

    uv run python scripts/screenshot.py            # every scene
    uv run python scripts/screenshot.py hero trace  # just these

One driver, so a UI change is one command away from a refreshed gallery.
Interactive features (tab rename, the command palette, /search, tool-call
compaction, markdown streaming, tab status colors) moved to ANIMATED demos
instead -- see scripts/record_gif.py -- since a single still can't show an
interaction; this file kept only the scenes a still genuinely suits.
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from datetime import datetime, timezone  # noqa: E402

from doxa.app import DoxaApp, ToolChip, TurnBlock  # noqa: E402
from doxa.engine import EngineEvent  # noqa: E402
from doxa.identity import Usage, UsageLimit  # noqa: E402
from doxa.peers import PeerInfo  # noqa: E402
from tests.fakes import FakeEngine  # noqa: E402

SHOTS = ROOT / "assets" / "shots"

# Every scene runs under this machine's real `claude` CLI config on disk --
# doxa.identity reads it directly (subscription tier, cached usage
# utilization). Screenshots must never leak whoever generated them, so the
# whole identity surface is patched to fixed, plausible FAKE values for the
# duration of every scene, not just the ones that show a status bar.
_FAKE_LOCAL_ACCOUNT = {"organizationRateLimitTier": "default_claude_max_20x"}
_FAKE_USAGE = Usage(
    session=UsageLimit(kind="session", percent=40, severity="normal", resets_at=""),
    weekly=UsageLimit(kind="weekly_all", percent=54, severity="normal", resets_at=""),
    scoped=None,
    scope_label="",
    fetched_at=datetime.now(timezone.utc),
)


def _fake_identity():
    return mock.patch.multiple(
        "doxa.identity",
        local_account=lambda: dict(_FAKE_LOCAL_ACCOUNT),
        usage=lambda: _FAKE_USAGE,
    )


def _peer(session_id: str, title: str, clients: "int | None") -> PeerInfo:
    """A plausible, entirely fake registry entry -- no real ids, no real
    cwd outside this checkout."""
    return PeerInfo(
        session_id=session_id, pid=42424, socket_path=f"/run/doxa/{session_id}.sock",
        cwd=str(ROOT), repo_root=str(ROOT), title=title,
        started_at="2026-08-23T08:14:00.000000Z",
        heartbeat_at="2026-08-23T09:58:00.000000Z",
        daemon_socket=f"/run/doxa/{session_id}.sock", clients=clients,
    )


async def _settle(pilot, turns: int = 40, step: float = 0.02) -> None:
    for _ in range(turns):
        await pilot.pause(step)


# --------------------------------------------------------------------- #
# Scene: hero -- the shell mid-conversation, three tabs, full status bar.
# --------------------------------------------------------------------- #

HERO_SCRIPT = [
    EngineEvent("turn_started", {}),
    # Split mid-table-row and mid-bold-span deliberately -- the real shape
    # of an LLM text_delta stream, and the shape the streaming Markdown
    # widget (v0.13.0, item c) has to survive intact. Also the one shot
    # that has to show a rendered table + bold text, not just prose.
    EngineEvent("text_delta", {"text": "Two beliefs about this repo are relevant here:\n\n"}),
    EngineEvent("text_delta", {"text": "| id | status | outcomes |\n|----|--------|----------|\n"}),
    EngineEvent("text_delta", {"text": "| **#184** | STEER | 12 |\n| #201 | CITE | 0 |\n\n"}),
    EngineEvent("text_delta", {"text": "**#184** is calibrated; #201 is cite-only "}),
    EngineEvent("text_delta", {"text": "until it earns a track record of its own."}),
    EngineEvent("tool_call", {"id": "t1", "name": "lore_belief_search",
                              "input": {"query": "deploy checklist", "scope": "project"}}),
    EngineEvent("tool_result", {"id": "t1", "name": "lore_belief_search",
                                "result_summary": "2 beliefs: #184 STEER (0.91 · 12 outcomes) · #201 CITE",
                                "is_error": False, "duration_ms": 45}),
    EngineEvent("turn_done", {"cost_usd": 0.0031, "duration_ms": 1840, "is_error": False,
                              "session_cost_usd": 0.0031, "ctx_percentage": 11.0}),
]


def _hero_engine() -> FakeEngine:
    peers = [
        _peer("f00dfeed", "paper draft session", clients=1),
        _peer("cafebabe1", "kg-stats refactor", clients=0),
    ]
    engine = FakeEngine(HERO_SCRIPT, model="claude-opus-4-5", peers=peers)
    engine.detachable = True
    engine.session_id = "a1b2c3d4e5"
    return engine


def _sibling_tab_factory() -> Callable[[], FakeEngine]:
    models = iter(["claude-sonnet-4-5", "claude-haiku-4-5"])

    def factory() -> FakeEngine:
        model = next(models, "claude-sonnet-4-5")
        engine = FakeEngine([], model=model)
        engine.detachable = True
        engine.session_id = f"{model[:6]}0000"
        return engine

    return factory


async def _drive_hero(app: DoxaApp, pilot) -> None:
    await pilot.pause()
    await app.action_new_tab()
    await pilot.pause()
    await app.action_new_tab()
    await pilot.pause()
    tabbed = app.query_one("#session-tabs")
    tabbed.active = app.panes()[0].id or tabbed.active
    await pilot.pause()
    app.query_one("#prompt-input").value = "what do we believe about deploys here?"
    await pilot.press("enter")
    for _ in range(200):
        blocks = list(app.query("TurnBlock"))
        if blocks and "earns a track record" in getattr(blocks[0], "assistant_text", ""):
            break
        await pilot.pause(0.02)
    await pilot.pause(0.2)


# --------------------------------------------------------------------- #
# Scene: trace -- a Task call's subagent activity nested under its chip.
# --------------------------------------------------------------------- #

TRACE_SCRIPT = [
    EngineEvent("turn_started", {}),
    EngineEvent("tool_call", {
        "id": "task-1", "name": "Task",
        "input": {"description": "explore the auth path", "subagent_type": "Explore"},
    }),
    EngineEvent("text_delta", {"text": "checking the token refresh path now", "parent_id": "task-1"}),
    EngineEvent("tool_call", {
        "id": "sub-1", "name": "Grep", "input": {"pattern": "refresh_token"},
        "parent_id": "task-1",
    }),
    EngineEvent("tool_result", {
        "id": "sub-1", "name": "Grep", "result_summary": "3 hits in doxa/auth.py",
        "is_error": False, "duration_ms": 12, "parent_id": "task-1",
    }),
    EngineEvent("text_delta", {"text": "The refresh path reuses the CLI's own cached token."}),
    EngineEvent("tool_result", {
        "id": "task-1", "name": "Task",
        "result_summary": "explored: refresh handled in doxa/auth.py, 3 call sites",
        "is_error": False, "duration_ms": 640,
    }),
    EngineEvent("turn_done", {
        "cost_usd": 0.0045, "duration_ms": 710, "is_error": False,
        "session_cost_usd": 0.0045, "ctx_percentage": 14.0,
    }),
]


async def _drive_trace(app: DoxaApp, pilot) -> None:
    await pilot.pause()
    app.query_one("#prompt-input").value = "how does token refresh work here?"
    await pilot.press("enter")
    chips = {}
    for _ in range(150):
        chips = {c.call_id: c for c in app.query(ToolChip)}
        task = chips.get("task-1")
        if task is not None and task.tool_result is not None:
            break
        await pilot.pause(0.02)
    task, sub = chips["task-1"], chips["sub-1"]
    # Chips now nest inside the turn's ONE "Tool calls (N)" section (item
    # b); expanding a chip does nothing VISIBLE while that section is
    # still collapsed (Collapsible hides its whole Contents, nested
    # chips included) -- open the section first, same as a real user
    # would have to before ever seeing the chips beneath it.
    app.query_one(TurnBlock).tool_section.collapsed = False
    await pilot.pause()
    task.collapsed = False
    await pilot.pause()
    sub.collapsed = False
    await pilot.pause()
    app.query_one("#block-list").scroll_end(animate=False)
    await pilot.pause()


# --------------------------------------------------------------------- #
# Scene: memory -- a lore tool chip opened up, showing STEER vs CITE.
# --------------------------------------------------------------------- #

async def _drive_memory(app: DoxaApp, pilot) -> None:
    await pilot.pause()
    app.query_one("#prompt-input").value = "what do we believe about deploys here?"
    await pilot.press("enter")
    for _ in range(150):
        chips = list(app.query(ToolChip))
        if chips and chips[0].tool_result is not None:
            break
        await pilot.pause(0.02)
    chip = list(app.query(ToolChip))[0]
    app.query_one(TurnBlock).tool_section.collapsed = False
    await pilot.pause()
    chip.collapsed = False
    await pilot.pause()


# --------------------------------------------------------------------- #
# Scene: settings -- the modal, effective values + provenance.
# --------------------------------------------------------------------- #

async def _drive_settings(app: DoxaApp, pilot) -> None:
    from doxa import config as config_mod

    await pilot.pause()
    os.environ["DOXA_NERD_FONT"] = "1"
    config_mod.invalidate()
    try:
        await pilot.press("ctrl+comma")
        await _settle(pilot, 10)
    finally:
        del os.environ["DOXA_NERD_FONT"]
        config_mod.invalidate()


# --------------------------------------------------------------------- #
# Scene: clock -- the upper-right clock (item M), date + seconds on,
# never displacing the tab bar it shares a row with.
# --------------------------------------------------------------------- #

# A fixed instant, not "now" -- a screenshot with a live clock in it would
# be a different PNG every time scripts/screenshot.py ran, which defeats
# the point of a committed gallery. doxa.clock.now_utc is the one seam
# built for exactly this (see its docstring).
_CLOCK_FROZEN_AT = datetime(2026, 8, 24, 14, 32, 7, tzinfo=timezone.utc)


async def _drive_clock(app: DoxaApp, pilot) -> None:
    from unittest import mock as mock_mod

    from doxa import config as config_mod

    await pilot.pause()
    await app.action_new_tab()
    await pilot.pause()
    env = {
        "DOXA_CLOCK_SHOW": "1", "DOXA_CLOCK_DATE": "1",
        "DOXA_CLOCK_SECONDS": "1", "DOXA_CLOCK_TZ": "UTC",
    }
    with mock_mod.patch.dict(os.environ, env), \
         mock_mod.patch("doxa.clock.now_utc", return_value=_CLOCK_FROZEN_AT):
        config_mod.invalidate()
        from doxa.app import ClockChip

        app.query_one(ClockChip).reconfigure()
        await pilot.pause()
    # reconfigure() re-read the (now-restored) real environment on exit
    # from the `with` above only if something ELSE calls it -- freeze the
    # rendered text in place for the screenshot rather than re-arming
    # against real wall-clock config.
    config_mod.invalidate()


# --------------------------------------------------------------------- #
# Scene: sessions -- /sessions output, attached + detached rows, peers chip.
# --------------------------------------------------------------------- #

async def _drive_sessions(app: DoxaApp, pilot) -> None:
    await pilot.pause()
    entries = [
        _peer("a1b2c3d4e5", "deploy checklist rewrite", clients=1),
        _peer("f00dfeed01", "kg-stats refactor", clients=0),
        _peer("cafebabe02", "onboarding notes", clients=0),
    ]
    pane = app.active_pane
    with mock.patch("doxa.peers.read_registry", return_value=entries):
        await pane._cmd_sessions("")
    await pilot.pause()


# --------------------------------------------------------------------- #

@dataclass
class Scene:
    name: str
    drive: Callable[[DoxaApp, object], Awaitable[None]]
    size: tuple[int, int] = (104, 32)
    engine_factory: "Callable[[], FakeEngine] | None" = None
    new_session_factory: "Callable[[], FakeEngine] | None" = None


# `size` is a (cols, rows) TERMINAL geometry, not a pixel one -- Textual's
# SVG export renders each cell at a fixed pixel size, so the two only line
# up once you know that size. Measured empirically off this checkout's
# actual export (render at 80x24, 160x24 and 80x48, convert with inkscape,
# read the PNGs back): width = 12.2*cols + 18, height = 24.375*rows + 51.
# The 24.375/12.2 ~= 2.0 cell-height-to-width ratio is a terminal cell
# being roughly twice as tall as it is wide, exactly as expected.
#
# Every shot targets 16:9 (chosen over 4:3 -- a terminal strip's own shape
# is already landscape, so 16:9 is the smaller stretch from a tab bar's
# natural aspect) within ~2%, picked by solving each formula for the ROW
# count at the scene's existing COLUMN count and rounding. Never the other
# way: shrinking cols to hit the ratio risks clipping the status bar and
# tab strip, which are not width-budgeted the way tab LABELS are (see
# TAB_LABEL_MAX in doxa/app.py) -- growing rows only ever adds blank
# canvas below existing content, never cuts anything off. `settings` is
# the one exception: its rows were already tall enough that shrinking them
# to hit 16:9 would have clipped the modal, so its COLUMNS grew instead.
SCENES: list[Scene] = [
    Scene("hero", _drive_hero, size=(172, 47), engine_factory=_hero_engine,
          new_session_factory=_sibling_tab_factory()),
    Scene("trace", _drive_trace, size=(172, 47),
          engine_factory=lambda: FakeEngine(TRACE_SCRIPT, model="claude-opus-4-5")),
    Scene("memory", _drive_memory, size=(172, 47),
          engine_factory=_hero_engine),
    Scene("settings", _drive_settings, size=(120, 32),
          engine_factory=lambda: FakeEngine([], model="claude-opus-4-5")),
    Scene("clock", _drive_clock, size=(120, 32),
          engine_factory=lambda: FakeEngine([], model="claude-opus-4-5"),
          new_session_factory=lambda: FakeEngine([], model="claude-sonnet-4-5")),
    Scene("sessions", _drive_sessions, size=(172, 47),
          engine_factory=_hero_engine),
]


async def _run_scene(scene: Scene) -> None:
    app = DoxaApp(
        cwd=str(ROOT),
        engine_factory=scene.engine_factory,
        new_session_factory=scene.new_session_factory,
    )
    with _fake_identity():
        async with app.run_test(size=scene.size) as pilot:
            await scene.drive(app, pilot)
            SHOTS.mkdir(parents=True, exist_ok=True)
            out = SHOTS / f"{scene.name}.svg"
            app.save_screenshot(str(out))
            print(f"saved {out}")


async def main(names: list[str]) -> None:
    wanted = set(names) or {scene.name for scene in SCENES}
    unknown = wanted - {scene.name for scene in SCENES}
    if unknown:
        raise SystemExit(f"unknown scene(s): {', '.join(sorted(unknown))}")
    for scene in SCENES:
        if scene.name in wanted:
            await _run_scene(scene)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))

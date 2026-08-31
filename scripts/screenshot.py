# SPDX-License-Identifier: AGPL-3.0-only
"""Render the README's screenshot gallery -- no live SDK, no spend.

Drives DoxaApp headlessly, one Textual pilot session per SCENE, each scene
scripting a FakeEngine (and, where a scene needs live-registry state the
app would otherwise read from disk, patching doxa.peers for the duration)
to put a real feature on screen: the shell mid-conversation, the trace
tree, a still-running subagent's second status row and its own tab, the
settings modal, /sessions, and a belief-search tool call opened up. Saves
each as assets/shots/<scene>.svg; convert to PNG with inkscape (CI does
not need to -- the SVGs are committed too):

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
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# v0.67.0: isolation, BEFORE any doxa import -- doxa.config's ROOT/
# PROJECTS_DIR (and doxa.setup's home paths) are read from the
# environment at IMPORT time, the same ordering constraint
# tests/conftest.py's own module docstring states for exactly this
# reason. Without this, two status-bar chips read straight off this
# machine's REAL, ambient state and neither is scripted anywhere in this
# file: `N proposals` (`doxa.ui.labels.staged_count`, over the real
# `lore_core.pending` spool) and `mem u%p%` (`memory_fill`, over the real
# curated-memory files) -- both bypass the FakeEngine entirely, reading
# lore_core's own on-disk store directly. MEASURED, not assumed: run
# without this, `N proposals` read 205 one run and 78 the next on this
# shared dev machine, changing between two invocations of the same
# script with no scene edited -- exactly the leak `_fake_identity` below
# already exists to close for account numbers, now closed for these two
# chips as well. `/settings` reads `DOXA_HOME`-backed config the same
# way, so that is isolated here too.
#
# ``setdefault``, not a bare assignment: tests/test_record_gif.py imports
# this module's sibling (scripts/record_gif.py, which carries the
# identical block) to check its SCENES registry, no rendering -- under
# pytest, conftest.py has ALREADY pointed these at ITS OWN throwaway
# directory before any test module is collected, and this must never
# clobber that isolation out from under the rest of the suite. Run
# standalone (`python scripts/screenshot.py`), nothing has set these yet,
# and setdefault establishes this file's own throwaway directory exactly
# as a bare assignment would have.
_tmp = Path(tempfile.mkdtemp(prefix="doxa-shots-"))
os.environ.setdefault("LORE_ROOT", str(_tmp / "lore"))
os.environ.setdefault("LORE_PROJECTS_DIR", str(_tmp / "projects"))
os.environ.setdefault("DOXA_RUNTIME_DIR", str(_tmp / "runtime"))
os.environ.setdefault("DOXA_HOME", str(_tmp / "doxa-home"))
os.environ.setdefault("XDG_CONFIG_HOME", str(_tmp / "xdg"))
os.environ.setdefault("DOXA_SKIP_FIRST_RUN", "1")

from datetime import datetime, timezone  # noqa: E402

from doxa.app import ChipPicker, DoxaApp, ToolChip, TurnBlock  # noqa: E402
from doxa.engine import EngineEvent, context_breakdown  # noqa: E402
from doxa.identity import Usage, UsageLimit  # noqa: E402
from doxa.peers import PeerInfo  # noqa: E402
from textual.widgets import TabbedContent  # noqa: E402
from tests.fakes import FakeEngine  # noqa: E402
from tests.helpers import _belief  # noqa: E402

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


async def _fill_hero_conversation(app: DoxaApp, pilot) -> None:
    """Three tabs and one real Q&A, run to completion in the first one --
    the SAME content `hero` itself shows, reused (v0.67.0) as background
    filling for every scene whose OWN subject (a modal, a clock chip, an
    error block) does not by itself reach 250x69: a shot that is two-
    thirds empty canvas around its subject reads as a mistake, and a
    plausible three-tab session actually doing something is honest filler
    where "the transcript behind it" is the whole ask -- never fabricated
    content, the exact same script this file already ships as `hero`."""
    await pilot.pause()
    await app.action_new_tab()
    await pilot.pause()
    await app.action_new_tab()
    await pilot.pause()
    tabbed = app.query_one("#session-tabs")
    # `tab_id`, not `id`. Until v0.91.0 a SessionPane WAS the TabPane, so
    # its own id was the tab strip's id and this line worked. Splits made
    # a tab a `PaneTab` CONTAINING panes, and the two ids diverged --
    # after which every scene in this file died with
    # `ValueError: No Tab with id '--content-tab-pane-1'` before saving
    # anything. Nobody noticed for three releases because nothing
    # regenerates this gallery except running it, and it was last run at
    # 0.87.0. Measured here, in the run that produced 0.94.0's assets.
    tabbed.active = app.panes()[0].tab_id or tabbed.active
    await pilot.pause()
    app.query_one("#prompt-input").value = "what do we believe about deploys here?"
    await pilot.press("enter")
    for _ in range(200):
        blocks = list(app.query("TurnBlock"))
        if blocks and "earns a track record" in getattr(blocks[0], "assistant_text", ""):
            break
        await pilot.pause(0.02)
    await pilot.pause(0.2)


async def _drive_hero(app: DoxaApp, pilot) -> None:
    await _fill_hero_conversation(app, pilot)


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
# Scene: transparent (v0.29.0, the background setting) -- the trace
# scene's own chips and tool-calls section, replayed with DOXA_BACKGROUND=
# transparent live-flipped through the exact path the settings modal uses
# (DoxaApp._apply_background + refresh_css), NOT a fresh app -- proving
# the live-toggle path, not just the boot path. Textual's SVG exporter has
# to bake SOME concrete color where the base is really the terminal's own
# (a static image cannot show real pass-through -- see README), so this
# shot exists to confirm the STRUCTURAL claim instead: the status bar, the
# tool-calls dimmer step and ToolChip's own raised/bordered chrome still
# read as distinct, painted steps once the base itself stops painting.
# --------------------------------------------------------------------- #


async def _drive_transparent(app: DoxaApp, pilot) -> None:
    from doxa import config as config_mod

    os.environ["DOXA_BACKGROUND"] = "transparent"
    try:
        config_mod.invalidate()
        app._apply_background()
        app.refresh_css(animate=False)
        await pilot.pause()
        await _drive_trace(app, pilot)
    finally:
        del os.environ["DOXA_BACKGROUND"]
        config_mod.invalidate()


# --------------------------------------------------------------------- #
# Scene: subagent-tracker -- the second status row + an open transcript
# tab, side by side with the trace tree the subagent will land in once it
# finishes. The Task call is left deliberately UNRESOLVED (no tool_result
# for it in the script) so the subagent is still "running" in the frozen
# shot -- that is the state the second line and the status chip exist for.
# --------------------------------------------------------------------- #

SUBAGENT_TRACKER_SCRIPT = [
    EngineEvent("turn_started", {}),
    EngineEvent("tool_call", {
        "id": "task-1", "name": "Task",
        "input": {"description": "audit the retry backoff logic", "subagent_type": "Explore"},
    }),
    EngineEvent("text_delta", {"text": "checking every retry call site", "parent_id": "task-1"}),
    EngineEvent("tool_call", {
        "id": "sub-1", "name": "Grep", "input": {"pattern": "backoff"},
        "parent_id": "task-1",
    }),
    EngineEvent("tool_result", {
        "id": "sub-1", "name": "Grep", "result_summary": "4 hits in doxa/engine.py",
        "is_error": False, "duration_ms": 9, "parent_id": "task-1",
    }),
    # task-1 itself never resolves -- the subagent is still running.
]


async def _drive_subagent_tracker(app: DoxaApp, pilot) -> None:
    await pilot.pause()
    pane = app.active_pane
    app.query_one("#prompt-input").value = "how does the retry backoff work here?"
    await pilot.press("enter")
    for _ in range(150):
        if "task-1" in pane._subagents and pane.query("#subagent-line"):
            break
        await pilot.pause(0.02)
    await pane.open_transcript("task-1")
    await pilot.pause()
    # Back to the session tab: the tab STRIP still shows the open
    # transcript tab (proving it exists, titled from the subagent's own
    # label), while the visible pane is the one carrying the second
    # status row this shot is really about.
    tabbed = app.query_one("#session-tabs", TabbedContent)
    tabbed.active = pane.tab_id or tabbed.active
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

    # Fills the frame BEHIND the modal (v0.67.0's uniform 250x69 -- see
    # WIDE's own docstring) with the same three-tab, real-Q&A session
    # `hero` shows: the modal itself stays its own designed width
    # (#settings-panel's `max-width: 100`, a deliberate cap unrelated to
    # screenshot geometry), so what fills the rest of a wide terminal
    # honestly is what is actually still visible around and through the
    # dimmed wash -- the tab strip and the status bar of a session doing
    # something, not blank canvas invented for the shot.
    await _fill_hero_conversation(app, pilot)
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

    # v0.67.0: the same three-tab, real-Q&A fill `settings` uses -- the
    # clock chip's own subject is one row of the status bar, and the rest
    # of a 250x69 frame is what needs the content, not the chip itself.
    await _fill_hero_conversation(app, pilot)
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
# Scenes: banner -- the opening block with the DOXA logo above the
# identity fields; image-support -- /img's measurement plus a render in
# every tier this terminal may honestly draw.
#
# Both force the HALF-BLOCK tier, and that is a constraint of the medium
# rather than a preference. Textual's SVG export writes CELLS: half-block
# glyphs survive it as real colored rectangles, while kitty-graphics and
# sixel are escape sequences a terminal interprets and an SVG has no
# representation for at all. So half-block is the only tier a still can
# capture -- which makes these shots a floor, not a ceiling. On a terminal
# that answers the graphics query the same banner is drawn at full pixel
# resolution, and `/img` in that terminal is where you see the difference.
# --------------------------------------------------------------------- #

_HALFBLOCK = {"DOXA_IMAGE_MODE": "halfblock"}


async def _drive_banner(app: DoxaApp, pilot) -> None:
    await _settle(pilot)
    # v0.67.0: two more tabs, for the same reason `settings`/`clock` fill
    # with a running conversation -- unlike those, running one HERE would
    # scroll the banner this scene exists to show right out of the frame
    # (it lives in the same scrolling block list a turn appends to), so
    # that filler is not honest for this surface. Two more tabs in the
    # strip is: each boots its own identical banner under the same env,
    # genuinely true of a multi-tab session, and the one thing available
    # here that does not touch the transcript. The blank canvas below the
    # identity block is real and left as itself, not padded with
    # anything invented -- see this file's own module docstring /
    # CHANGELOG for the explicit call-out.
    await app.action_new_tab()
    await pilot.pause()
    await app.action_new_tab()
    await pilot.pause()
    tabbed = app.query_one("#session-tabs")
    tabbed.active = app.panes()[0].tab_id or tabbed.active
    await pilot.pause()


async def _drive_banner_degraded(app: DoxaApp, pilot) -> None:
    await _settle(pilot)
    # Same reasoning as _drive_banner just above.
    await app.action_new_tab()
    await pilot.pause()
    await app.action_new_tab()
    await pilot.pause()
    tabbed = app.query_one("#session-tabs")
    tabbed.active = app.panes()[0].tab_id or tabbed.active
    await pilot.pause()


async def _drive_image_support(app: DoxaApp, pilot) -> None:
    await _settle(pilot)
    await app.active_pane._cmd_img("")
    await _settle(pilot)


# --------------------------------------------------------------------- #
# Scene: error-block -- v0.53.0's whole point, on screen: a caught
# exception rendered AS PART OF THE TRANSCRIPT, collapsed to one line by
# default and expanded here to show the fold underneath it, instead of
# taking the app down.
# --------------------------------------------------------------------- #


async def _drive_error_block(app: DoxaApp, pilot) -> None:
    from doxa.app import ErrorBlock

    # v0.67.0: the same three-tab, real-Q&A fill `settings`/`clock` use --
    # the error block reads as itself sitting BELOW a real turn, not
    # floating alone in an otherwise-empty transcript.
    await _fill_hero_conversation(app, pilot)
    try:
        raise TimeoutError("lore_belief_search timed out after 30s")
    except TimeoutError as exc:
        app.report_exception(
            exc, origin="LORE", context="tool call: lore_belief_search",
        )
    await pilot.pause()
    block = app.query_one(ErrorBlock)
    block.collapsed = False
    await pilot.pause()


# --------------------------------------------------------------------- #
# Scene: context (v0.87.0, NEW) -- `/context` as v0.81.0 redrew it: a
# FIXED 10x20 grid of 200 draughts cells at 0.5% each, model and headline
# beside the top rows, the per-category legend beside the lower ones, and
# the source sections below. This replaced v0.75.0's proportional bar, and
# no asset in this gallery has ever shown either one -- the bar shipped and
# was retired inside four releases without a scene, so this is the first
# capture of the surface rather than a refresh of a stale one.
#
# GLYPHS, not ascii: `context_grid` (DOXA_CONTEXT_GRID) defaults to the
# draughts cells (⛀ ⛁ ⛶) and the ascii tier ([#]/[ ]) is the fallback for
# a font that cannot draw them. A gallery shows the DEFAULT -- the env var
# is deliberately left unset here so this scene reads whatever a fresh
# install really renders, and both tiers are 3 columns wide anyway, so the
# layout is identical either way.
# --------------------------------------------------------------------- #

# Shaped exactly like the SDK's own ContextUsageResponse -- the same shape
# tests/test_context.py pins -- and normalized through the ONE path both
# real engines use (`doxa.engine.context_breakdown`), so this scene renders
# the identical structure a live session hands the block. Every number is
# invented and plausible; none is measured off this machine, the same rule
# `_fake_identity` holds for the status bar. `/context` is a diagnostic
# surface, so what it must never do is show a breakdown DOXA estimated --
# and it does not: the categories below stand in for the CLI's own
# accounting, which is the only thing this command has ever printed.
CONTEXT_USAGE = {
    "categories": [
        {"name": "System prompt", "tokens": 4_150, "color": "blue"},
        {"name": "System tools", "tokens": 13_400, "color": "green"},
        {"name": "MCP tools", "tokens": 2_060, "color": "purple"},
        {"name": "Memory files", "tokens": 2_400, "color": "orange"},
        {"name": "Messages", "tokens": 38_900, "color": "red"},
        {"name": "Free space", "tokens": 119_090, "color": "grey"},
    ],
    "totalTokens": 60_910,
    "maxTokens": 180_000,
    "rawMaxTokens": 200_000,
    "percentage": 33.8,
    "model": "claude-opus-4-5",
    "isAutoCompactEnabled": True,
    "autoCompactThreshold": 160_000,
    "memoryFiles": [
        {"path": "~/Schreibtisch/doxa/CLAUDE.md", "type": "Project", "tokens": 2_400},
    ],
    "mcpTools": [
        {"name": "lore_belief_search", "serverName": "doxa_lore",
         "tokens": 380, "isLoaded": True},
        {"name": "lore_belief_show", "serverName": "doxa_lore",
         "tokens": 420, "isLoaded": True},
        {"name": "lore_belief_neighbours", "serverName": "doxa_lore",
         "tokens": 460, "isLoaded": True},
    ],
    "agents": [
        {"agentType": "Explore", "source": "builtin", "tokens": 480},
        {"agentType": "Plan", "source": "builtin", "tokens": 320},
    ],
}


async def _drive_context(app: DoxaApp, pilot) -> None:
    # The same three-tab, real-Q&A fill `settings`/`clock`/`error-block`
    # use: /context mounts its block at the END of the transcript, so what
    # sits above it in a 69-row frame should be a session that has actually
    # been consuming the window this grid is measuring -- not blank canvas
    # under a block claiming 33.8% of a context is in use.
    await _fill_hero_conversation(app, pilot)
    pane = app.active_pane
    pane.engine.context_usage_result = context_breakdown(CONTEXT_USAGE)
    await pane._cmd_context("")
    await _settle(pilot, 12)
    app.query_one("#block-list").scroll_end(animate=False)
    await pilot.pause()


# --------------------------------------------------------------------- #
# Scene: beliefs-picker (v0.87.0, NEW FILE -- see below) -- the beliefs
# surface as it actually exists now: the shared ChipPicker, opened off the
# beliefs chip.
#
# This REPLACES `beliefs-browser.png`/`.svg`, which this release deletes.
# Those two did not merely drift -- they showed the standalone beliefs
# BROWSER TAB, a whole surface v0.69.0 removed and v0.73.0 finished
# removing (`/beliefs` opens this picker instead; `_beliefs_tab` and its
# Ctrl+W/Ctrl+Q cases are gone). Their generating scene went with it, which
# is why the files sat in `assets/shots/` for eighteen releases with
# nothing in either script able to refresh them. A file named for a removed
# feature is a claim that the feature is still there, so the name goes too;
# nothing in README.md or docs/ ever referenced either path (checked, not
# assumed), so no link breaks.
#
# What this scene has to show, and what the old pair could not:
#   * FIXED COLUMNS -- PICKER_STAMP_COL 15 / PICKER_STATUS_COL 28 /
#     PICKER_AGE_COL 7, one 50-column prefix every row starts its text
#     after (v0.67.0 gave the two pickers one row shape; v0.77.0 re-checked
#     the three widths against both row types). The old image predates all
#     of it and shows the `·`-joined string that drifted with each field's
#     length -- the reported "the proposals view should be formatted that
#     the columns have fixed width".
#   * The `g graph` row action (v0.86.0), beside y/c/s/r.
# --------------------------------------------------------------------- #

# `_belief` is tests/helpers.py's own fixture builder -- the SAME shape
# `SessionEngine.list_beliefs` hands the picker, reused rather than
# re-declared here so this scene cannot drift from what the picker's
# several hundred tests are written against. Stamps are relative to now
# (that is what `_belief` does), so the age column reads as a real age
# rather than a frozen one; the claims themselves are invented, same
# discipline as every other scene in this file.
def _beliefs() -> list[dict]:
    return [
        _belief(184, "deploys are gated on the release checklist, not on CI alone",
                subject="project:doxa", created_days=180, idle_days=2,
                outcome="confirmed", outcome_days=1, outcomes=12, evidence_count=7),
        _belief(201, "the gallery's geometry constants live in scripts/screenshot.py",
                subject="project:doxa", created_days=90, idle_days=6,
                outcomes=0, evidence_count=3),
        _belief(232, "uv, never pip, for this repo",
                subject="project:doxa", created_days=140, idle_days=1,
                outcome="confirmed", outcome_days=3, outcomes=5, evidence_count=4),
        _belief(248, "worktrees are pruned weekly, so a stale one is a bug",
                subject="project:doxa", created_days=60, idle_days=11,
                outcome="contradicted", outcome_days=4, outcomes=3,
                evidence_count=2),
        _belief(261, "record_gif.py shares screenshot.py's fake-identity helpers",
                subject="project:doxa", created_days=45, idle_days=19,
                outcomes=0, evidence_count=1),
        _belief(117, "prefers fixed-column tables over ad hoc separator joins",
                subject="user", created_days=220, idle_days=8,
                outcome="confirmed", outcome_days=2, outcomes=9, evidence_count=6),
        _belief(129, "reads the CHANGELOG before the diff",
                subject="user", created_days=150, idle_days=27,
                outcomes=0, evidence_count=2),
    ]


async def _drive_beliefs_picker(app: DoxaApp, pilot) -> None:
    await pilot.pause()
    pane = app.active_pane
    beliefs = _beliefs()
    pane.engine.list_beliefs_result = beliefs
    # The status bar's `N beliefs` chip and this picker read two DIFFERENT
    # engine calls by design -- `belief_count()` is the cheap COUNT(*) that
    # runs on every status refresh, `list_beliefs()` the heavy claim-text
    # query only a click may make (open_beliefs_picker's own cost-discipline
    # note). FakeEngine hardcodes the count at 3, so left alone this shot
    # would show a chip reading `3 beliefs` above a picker listing seven,
    # and a reader has no way to know that disagreement is a fixture
    # artifact rather than a bug in the pair. Overridden on the INSTANCE,
    # before the first status refresh, rather than by editing
    # tests/fakes.py -- that file is the suite's, and a screenshot may not
    # move a number several hundred tests are written against. Set here so
    # the two agree for the same reason `_belief_cap_note` exists: a count
    # and a list that disagree must mean the cap bit, never the scaffolding.
    pane.engine.belief_count = lambda: len(beliefs)
    await _fill_hero_conversation(app, pilot)
    await pane.open_beliefs_picker()
    for _ in range(200):
        picker = pane.query_one("#chip-picker", ChipPicker)
        if picker.is_open:
            break
        await pilot.pause(0.02)
    await _settle(pilot, 12)


# --------------------------------------------------------------------- #
# Scene: split-panes (v0.94.0, NEW FILE -- the feature is v0.91.0) -- two
# INDEPENDENT sessions side by side inside ONE tab.
#
# That independence is the whole claim and the reason a still can carry
# it at all: `/vsplit` (Ctrl+N) does not open a second VIEW onto the
# session you were in, it spawns a second session through the same
# `new_session_factory` Ctrl+T uses (DoxaApp.split_active_pane) -- so the
# two panes have their own transcripts, their own models, their own
# status bars and their own cost lines, and the shot shows exactly that.
# The split is driven by the REAL key (`pilot.press("ctrl+n")`), the same
# "exercise the actual trigger" discipline scripts/record_gif.py's
# double-click rename and branch-chip click already follow, rather than
# by calling `split_active_pane` the way tests/test_split_panes.py does.
#
# Focus is left where the app put it -- in the NEW pane -- because that
# is v0.91.0's stated rule (a user who just asked for a second pane is
# asking to work in it) and a shot that quietly moved it back would be
# showing a state the feature never produces.
# --------------------------------------------------------------------- #

SPLIT_SECOND_SCRIPT = [
    EngineEvent("turn_started", {}),
    EngineEvent("text_delta", {"text": "Three surfaces shipped since the last gallery pass "}),
    EngineEvent("text_delta", {"text": "and have no asset yet:\n\n"}),
    EngineEvent("text_delta", {"text": "1. **split panes** (v0.91.0)\n"}),
    EngineEvent("text_delta", {"text": "2. **the live diff** (v0.92.0)\n"}),
    EngineEvent("text_delta", {"text": "3. **the folder chip** (v0.93.0)\n\n"}),
    EngineEvent("text_delta", {"text": "You are looking at the first of them: this pane and "}),
    EngineEvent("text_delta", {"text": "the one beside it are two separate sessions."}),
    EngineEvent("turn_done", {"cost_usd": 0.0022, "duration_ms": 1120, "is_error": False,
                              "session_cost_usd": 0.0022, "ctx_percentage": 6.0}),
]


def _split_tab_factory() -> Callable[[], FakeEngine]:
    """Engines for every pane this scene creates AFTER the first: the two
    sibling tabs `_fill_hero_conversation` opens, and then the split leaf
    itself. All three carry SPLIT_SECOND_SCRIPT; only the split leaf is
    ever handed a prompt, so only it renders a turn."""
    models = iter(["claude-sonnet-4-5", "claude-haiku-4-5", "claude-sonnet-4-5"])
    seq = iter(range(1, 99))

    def factory() -> FakeEngine:
        model = next(models, "claude-sonnet-4-5")
        engine = FakeEngine(SPLIT_SECOND_SCRIPT, model=model)
        engine.detachable = True
        engine.session_id = f"{model[:6]}{next(seq):04d}"
        return engine

    return factory


async def _until(pilot, cond: Callable[[], bool], tries: int = 250) -> bool:
    """tests/test_split_panes.py's own `_wait`, borrowed: poll a condition
    THIS driver already guaranteed will become true, never a bet on
    timing content into existence."""
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


async def _drive_split_panes(app: DoxaApp, pilot) -> None:
    await _fill_hero_conversation(app, pilot)
    left = app.active_pane
    await pilot.press("ctrl+n")
    assert await _until(pilot, lambda: app.active_pane is not left), (
        "ctrl+n did not create a second pane"
    )
    right = app.active_pane
    assert right is not None
    assert await _until(pilot, lambda: right.region.width > 0), (
        "the new pane never painted"
    )
    prompt = right.query_one("#prompt-input")
    prompt.focus()
    prompt.value = "which gallery scenes still need a capture at 0.94.0?"
    await pilot.press("enter")
    assert await _until(
        pilot,
        lambda: any(
            "first of them" in getattr(b, "assistant_text", "")
            for b in right.query(TurnBlock)
        ),
    ), "the second pane's turn never finished"
    await _settle(pilot, 12)


# --------------------------------------------------------------------- #
# Scene: live-diff (v0.94.0, NEW FILE -- the feature is v0.92.0) -- the
# spec's own design check, run for real: SESSION LEFT, DIFF RIGHT, both
# live, in one tab.
#
# The only scene in this file that is NOT rooted in this checkout (see
# `Scene.cwd_factory`), and it cannot be: `doxa.diff.compute` shells out
# to `git diff` for real, so the hunks below are hunks git itself
# produced from a real commit and a real edit on top of it. A scripted
# stand-in would be a picture of a diff rather than a picture of THE
# diff. The repo is built fresh under this module's own throwaway temp
# root, so nothing outside it is read or written.
#
# The PENDING-REJECTION BADGE is the second thing this shot is for, and
# it exists only mid-turn: `DiffPane.reject` reverts immediately when the
# session is idle, and QUEUES (badge, disabled button, nothing moved on
# disk) when a turn is in flight, because a rejection the user has
# clicked and cannot see the effect of is the worst of the three answers
# the spec weighed. So the second turn here genuinely never finishes --
# `_DiffEngine` holds it open -- rather than `pane.turn_in_flight` being
# poked to a value no real turn is in.
# --------------------------------------------------------------------- #

_AUTH_BASE = """\
# SPDX-License-Identifier: AGPL-3.0-only
\"\"\"Token refresh against the claude CLI's own cached OAuth session.\"\"\"

from __future__ import annotations

GRACE_SECONDS = 300


def expires_within(expiry: float, now: float) -> bool:
    return expiry - now <= GRACE_SECONDS


def refresh_token(cached: dict, now: float) -> dict:
    \"\"\"Renew inside the grace window, never after it.\"\"\"
    if not expires_within(cached["expires_at"], now):
        return cached
    return _renew(cached)


def _renew(cached: dict) -> dict:
    token = cli_refresh(cached["refresh_token"])
    return {**cached, **token}


def cli_refresh(token: str) -> dict:
    raise NotImplementedError
"""

_DIFF_SESSION_ID = "a1b2c3d4e5"
_DIFF_BRANCH = "doxa/a1b2c3d4"


def _diff_repo() -> str:
    """A real git worktree with a real base commit, two edits ten lines
    apart in one file (so git's default three lines of context puts them
    in SEPARATE hunks -- the whole "reject one, keep the other" claim
    rests on that separation being real) and one created file. Plus the
    DOXA sidecar `doxa.worktrees.read_meta` reads, so the diff resolves
    its base the way a real session's does instead of falling back to
    HEAD."""
    import json
    import subprocess

    from doxa import worktrees as worktrees_mod

    work = _tmp / "repos" / "doxa"
    work.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(work), check=True,
                       capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "demo@example.invalid")
    git("config", "user.name", "demo")
    (work / "doxa").mkdir(exist_ok=True)
    (work / "doxa" / "auth.py").write_text(_AUTH_BASE, encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("checkout", "-q", "-b", _DIFF_BRANCH)

    edited = _AUTH_BASE.replace(
        "GRACE_SECONDS = 300", "GRACE_SECONDS = 900"
    ).replace(
        '    token = cli_refresh(cached["refresh_token"])',
        '    token = cli_refresh(cached["refresh_token"])\n'
        '    if not token:\n'
        '        raise RefreshFailed("the CLI returned no token")',
    )
    (work / "doxa" / "auth.py").write_text(edited, encoding="utf-8")
    (work / "tests").mkdir(exist_ok=True)
    (work / "tests" / "test_grace_window.py").write_text(
        "# SPDX-License-Identifier: AGPL-3.0-only\n"
        "from doxa.auth import expires_within\n\n\n"
        "def test_renews_inside_the_window():\n"
        "    assert expires_within(900.0, 1.0)\n",
        encoding="utf-8",
    )

    meta = worktrees_mod.worktrees_root() / ".meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / f"{work.name}.json").write_text(json.dumps({
        "main_root": str(work), "session_id": _DIFF_SESSION_ID,
        "base_ref": "main", "branch": _DIFF_BRANCH,
    }), encoding="utf-8")
    return str(work)


class _DiffEngine(FakeEngine):
    """`hero`'s own script for the first prompt; the second prompt starts
    a turn that never finishes.

    Deliberate, and the reason this class exists rather than a
    `turn_in_flight = True` assignment: the pending badge is only ever
    shown while a turn is genuinely in flight, and this scene has to be
    in that state for real -- through `SessionPane._run_turn`, the same
    door a typed prompt goes down -- for the shot to be a picture of the
    feature rather than of a flag."""

    def __init__(self, cwd: str) -> None:
        super().__init__(HERO_SCRIPT, model="claude-opus-4-5", cwd=cwd)
        self.detachable = True
        self.session_id = _DIFF_SESSION_ID
        self._turns = 0

    async def send(self, prompt: str):
        self._turns += 1
        if self._turns == 1:
            async for event in super().send(prompt):
                yield event
            return
        yield EngineEvent("turn_started", {})
        await asyncio.Event().wait()  # held open for the rest of the scene


def _diff_sibling_factory() -> Callable[[], FakeEngine]:
    """The two sibling tabs `_fill_hero_conversation` opens behind this
    scene. Plain engines with no script -- they are strip filler, and
    they carry their OWN session ids so `DoxaApp.diff_pane_for` cannot
    match the wrong pane's diff."""
    models = iter(["claude-sonnet-4-5", "claude-haiku-4-5"])
    seq = iter(range(1, 99))

    def factory() -> FakeEngine:
        model = next(models, "claude-sonnet-4-5")
        engine = FakeEngine([], model=model, cwd=str(_tmp / "repos" / "doxa"))
        engine.detachable = True
        engine.session_id = f"{model[:6]}{next(seq):04d}"
        return engine

    return factory


async def _drive_live_diff(app: DoxaApp, pilot) -> None:
    from doxa.ui.diffview import DiffPane, FileSection, HunkView

    await _fill_hero_conversation(app, pilot)
    pane = app.active_pane
    assert pane is not None
    assert await _until(pilot, lambda: bool(pane._session_id)), "no session id"

    await pilot.press("f2")
    assert await _until(
        pilot,
        lambda: (
            app.query(DiffPane)
            and next(iter(app.query(DiffPane))).region.width > 0
            and "reading the diff"
            not in str(next(iter(app.query(DiffPane)))._head.renderable)
        ),
    ), "the diff pane never painted"
    diff = next(iter(app.query(DiffPane)))
    assert diff.region.x >= pane.region.x + pane.region.width, (
        "the diff did not land to the RIGHT of the session"
    )

    assert await _until(pilot, lambda: len(diff.query(FileSection)) == 2), (
        f"expected 2 files, got {[s.file_diff.path for s in diff.query(FileSection)]}"
    )
    section = next(
        s for s in diff.query(FileSection) if s.file_diff.path.endswith("auth.py")
    )
    section.collapsed = False
    assert await _until(pilot, lambda: len(section.query(HunkView)) == 2), (
        "the two edits did not parse as two hunks"
    )

    # A second turn, started for real and never finished -- the state the
    # pending badge exists for, reached through the same door a typed
    # prompt uses rather than by writing `turn_in_flight`.
    prompt = pane.query_one("#prompt-input")
    prompt.focus()
    prompt.value = "widen the grace window and make a missing token loud"
    await pilot.press("enter")
    assert await _until(pilot, lambda: pane.turn_in_flight), "no turn in flight"

    await diff.reject(section.query(HunkView)[0])
    assert await _until(
        pilot, lambda: bool(diff.queued) and section.query(HunkView)[0]._pending.display
    ), "the rejection did not queue with a visible badge"
    await _settle(pilot, 12)


# --------------------------------------------------------------------- #
# Scene: folder-chip (v0.94.0, NEW FILE -- the feature is v0.93.0) -- the
# leftmost identity chip when a session is NOT in a git repository:
# `dir NAME`, deliberately a DIFFERENT SHAPE from the git chip's
# `repo ⎇ branch @sha` rather than the same one with an empty branch half
# (doxa.session.chips.PaneChipsMixin._status_chips). Reported as "if i
# start in a non-repo dir, there is no folder/repo chip shown in the
# status line", and before v0.93.0 there was simply no chip at all.
#
# Rooted outside this checkout for the one reason no scene before it
# ever needed `cwd_factory`: this repository cannot be made not to be a
# repository, and the chip only exists where there is no `.git` above the
# session at all.
#
# `/dir` and `/cd` are in the same shot on purpose -- they are the pair
# v0.93.0 shipped, and `/cd` with no argument is the surface that
# explains, in the app itself, why a running session's own directory
# cannot move (the claude CLI subprocess was spawned with an OS-level cwd
# nothing can hand it a new one for).
# --------------------------------------------------------------------- #

FOLDER_SCRIPT = [
    EngineEvent("turn_started", {}),
    EngineEvent("text_delta", {"text": "There are three notes here:\n\n"}),
    EngineEvent("text_delta", {"text": "| file | topic |\n|------|-------|\n"}),
    EngineEvent("text_delta", {"text": "| `gallery.md` | which scenes still need a capture |\n"}),
    EngineEvent("text_delta", {"text": "| `bullets.md` | the README rewrite, measured |\n"}),
    EngineEvent("text_delta", {"text": "| `release.md` | the 0.94.0 chapter order |\n\n"}),
    EngineEvent("text_delta", {"text": "None of this is a git repository, so this session "}),
    EngineEvent("text_delta", {"text": "has no branch to be on."}),
    EngineEvent("turn_done", {"cost_usd": 0.0014, "duration_ms": 760, "is_error": False,
                              "session_cost_usd": 0.0014, "ctx_percentage": 5.0}),
]


def _plain_folder() -> str:
    """A plain directory with no `.git` anywhere above it -- under this
    module's own throwaway temp root, which is exactly why it qualifies."""
    folder = _tmp / "design-notes"
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in (
        ("gallery.md", "# gallery\n\nsplit panes, the live diff, the folder chip\n"),
        ("bullets.md", "# bullets\n\n2,511 characters across eleven bullets\n"),
        ("release.md", "# release\n\n0.94.0, in chapters\n"),
    ):
        (folder / name).write_text(body, encoding="utf-8")
    return str(folder)


def _folder_engine() -> FakeEngine:
    engine = FakeEngine(FOLDER_SCRIPT, model="claude-opus-4-5")
    engine.detachable = True
    engine.session_id = "b7e2f9a1c4"
    return engine


async def _drive_folder_chip(app: DoxaApp, pilot) -> None:
    await pilot.pause()
    await app.action_new_tab()
    await pilot.pause()
    tabbed = app.query_one("#session-tabs", TabbedContent)
    tabbed.active = app.panes()[0].tab_id or tabbed.active
    await pilot.pause()
    pane = app.active_pane
    assert pane is not None
    prompt = pane.query_one("#prompt-input")
    prompt.focus()
    prompt.value = "what's in this folder?"
    await pilot.press("enter")
    assert await _until(
        pilot,
        lambda: any(
            "no branch to be on" in getattr(b, "assistant_text", "")
            for b in pane.query(TurnBlock)
        ),
    ), "the folder turn never finished"
    await pane._cmd_dir("")
    await pilot.pause()
    await pane._cmd_cd("")
    await pilot.pause()
    app.query_one("#block-list").scroll_end(animate=False)
    await _settle(pilot, 12)


# --------------------------------------------------------------------- #

@dataclass
class Scene:
    name: str
    drive: Callable[[DoxaApp, object], Awaitable[None]]
    size: tuple[int, int] = (104, 32)
    engine_factory: "Callable[[], FakeEngine] | None" = None
    new_session_factory: "Callable[[], FakeEngine] | None" = None
    env: "dict[str, str] | None" = None
    """Environment for the whole scene, set BEFORE the app is constructed
    and removed after. The image scenes need it: the banner is mounted
    during pane boot, which is over before `drive` gets its first pause,
    so forcing the tier from inside a driver would be too late."""
    cwd_factory: "Callable[[], str] | None" = None
    """Where the app is rooted, built fresh for the scene, defaulting to
    THIS checkout (every scene before v0.94.0). Two scenes need somewhere
    else and neither can fake it: `live-diff` runs `git diff` for real
    against a real worktree with real edits in it, and `folder-chip`
    exists precisely to show the chip a session gets when it is NOT in a
    git repository at all -- which this checkout can never be. Built
    under this module's own throwaway temp root (see the isolation block
    at the top), so nothing outside it is read or written."""


# `size` is a (cols, rows) TERMINAL geometry, not a pixel one -- Textual's
# SVG export renders each cell at a fixed pixel size, so the two only line
# up once you know that size. Measured empirically off this checkout's
# actual export (render at 80x24, 160x24 and 80x48, convert with inkscape,
# read the PNGs back): width = 12.2*cols + 18, height = 24.375*rows + 51.
# The 24.375/12.2 ~= 2.0 cell-height-to-width ratio is a terminal cell
# being roughly twice as tall as it is wide, exactly as expected.
#
# v0.67.0: EVERY scene in this file now shares ONE geometry, `WIDE`
# below -- not a per-scene choice any more. The README gallery visibly
# changed pixel size scene to scene (five different sizes measured across
# what shipped before this release, `beliefs-browser` a sixth of its
# own); "every one is within 2% of 16:9" was exactly why that slipped
# through, since ratio uniformity is not size uniformity. Ratio still
# matters (16:9, chosen over 4:3 -- a terminal strip's own shape is
# already landscape, so 16:9 is the smaller stretch from a tab bar's
# natural aspect), but now it is solved ONCE, for the widest floor every
# scene has to clear, rather than once per scene.
#
# THE FLOOR: 250 columns. Measured, not assumed -- the permission-mode
# chip, curated-memory fill and staged-proposals chip all shipped after
# 172 (the previous shared width) was chosen, and a live status bar's own
# plain text -- read back off the widget, not off a guess -- now runs to
# 228 characters on `hero` (three open tabs, a worktree branch, peers and
# a session handle) against 168 content columns at 172, i.e. the row was
# losing everything from the memory chip on. `hero`/`trace`/`transparent`/
# `subagent-tracker`/`memory`/`sessions`/`image-support` -- the seven
# scenes that carry a live status bar -- are what actually NEED 250; every
# other scene here is held to the same number for uniformity, not because
# its own content demands it.
#
# THE ROWS: solved for 16:9 at 250 columns, the same way each scene used
# to solve its own -- height = width * 9/16, then invert `height =
# 24.375*rows + 51` and round: (3068*9/16 - 51) / 24.375 ~= 68.7 -> 69.
# 3068x1734 at 69 rows is 1.7693 against 16:9's 1.7778, 0.48% off, inside
# the ~2% tolerance every prior scene held to -- and happens to be exactly
# the WIDE the seven status-bar scenes already used, so THEY need no
# change at all; every other scene grows to match instead.
#
# WHERE A SCENE HAD LESS CONTENT THAN 250x69 -- content is added, never
# left as blank canvas (a shot that is two-thirds background reads as a
# mistake):
# `settings`/`clock`/`error-block` now run the SAME three-tab, real Q&A
# `hero` shows underneath their own subject (see
# `_fill_hero_conversation`); `beliefs-browser` (whose own bespoke
# 134x36 this release retires -- uniformity now outranks the content-fit
# case it was solving) gets a substantially larger scripted store instead
# of three beliefs and two proposals. `banner`/`banner-blocks` get two
# more tabs in the strip but NOT a running conversation -- one would
# scroll the boot banner these two scenes exist to show right out of the
# frame, since it lives in the same scrolling block list a turn appends
# to. Their own body below the identity block stays genuinely blank: a
# compact boot screen has nothing more truthful to add without inventing
# it, and this is named here rather than padded over.
WIDE = (250, 69)


SCENES: list[Scene] = [
    Scene("hero", _drive_hero, size=WIDE, engine_factory=_hero_engine,
          new_session_factory=_sibling_tab_factory()),
    Scene("trace", _drive_trace, size=WIDE,
          engine_factory=lambda: FakeEngine(TRACE_SCRIPT, model="claude-opus-4-5")),
    Scene("transparent", _drive_transparent, size=WIDE,
          engine_factory=lambda: FakeEngine(TRACE_SCRIPT, model="claude-opus-4-5")),
    Scene("subagent-tracker", _drive_subagent_tracker, size=WIDE,
          engine_factory=lambda: FakeEngine(SUBAGENT_TRACKER_SCRIPT, model="claude-opus-4-5")),
    Scene("memory", _drive_memory, size=WIDE,
          engine_factory=_hero_engine),
    Scene("settings", _drive_settings, size=WIDE,
          engine_factory=_hero_engine, new_session_factory=_sibling_tab_factory()),
    Scene("clock", _drive_clock, size=WIDE,
          engine_factory=_hero_engine, new_session_factory=_sibling_tab_factory()),
    Scene("sessions", _drive_sessions, size=WIDE,
          engine_factory=_hero_engine),
    Scene("error-block", _drive_error_block, size=WIDE,
          engine_factory=_hero_engine, new_session_factory=_sibling_tab_factory()),
    Scene("context", _drive_context, size=WIDE,
          engine_factory=_hero_engine, new_session_factory=_sibling_tab_factory()),
    Scene("beliefs-picker", _drive_beliefs_picker, size=WIDE,
          engine_factory=_hero_engine, new_session_factory=_sibling_tab_factory()),
    Scene("split-panes", _drive_split_panes, size=WIDE,
          engine_factory=_hero_engine, new_session_factory=_split_tab_factory()),
    Scene("live-diff", _drive_live_diff, size=WIDE,
          cwd_factory=_diff_repo,
          engine_factory=lambda: _DiffEngine(str(_tmp / "repos" / "doxa")),
          new_session_factory=_diff_sibling_factory()),
    Scene("folder-chip", _drive_folder_chip, size=WIDE,
          cwd_factory=_plain_folder,
          engine_factory=_folder_engine,
          new_session_factory=_sibling_tab_factory()),
    # THE banner, since v0.70.0 dropped the raster tier: every terminal
    # draws this. The scene name is kept rather than renamed to `banner`
    # so the README's asset path and caption stay valid; there is no
    # second form left for it to be distinguished from. v0.67.0 retired
    # its bespoke 95x25 for the shared WIDE.
    Scene("banner-blocks", _drive_banner_degraded, size=WIDE,
          engine_factory=lambda: FakeEngine([], model="claude-opus-4-5"),
          env={"DOXA_IMAGE_MODE": "halfblock"}),
    Scene("image-support", _drive_image_support, size=WIDE,
          engine_factory=lambda: FakeEngine([], model="claude-opus-4-5"),
          env=_HALFBLOCK),
]


async def _run_scene(scene: Scene) -> None:
    # Scene env is applied around app CONSTRUCTION, not just the drive:
    # DoxaApp.__init__ settles the image probe and the pane mounts its
    # banner during boot, both before a driver's first pause.
    with mock.patch.dict(os.environ, scene.env or {}):
        cwd = scene.cwd_factory() if scene.cwd_factory is not None else str(ROOT)
        app = DoxaApp(
            cwd=cwd,
            engine_factory=scene.engine_factory,
            new_session_factory=scene.new_session_factory,
        )
        await _export(app, scene)


async def _export(app: DoxaApp, scene: Scene) -> None:
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

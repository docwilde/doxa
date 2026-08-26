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

from doxa.app import DoxaApp, ToolChip, TurnBlock  # noqa: E402
from doxa.engine import EngineEvent  # noqa: E402
from doxa.identity import Usage, UsageLimit  # noqa: E402
from doxa.peers import PeerInfo  # noqa: E402
from textual.widgets import TabbedContent  # noqa: E402
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
    tabbed.active = pane.id or tabbed.active
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
    tabbed.active = app.panes()[0].id or tabbed.active
    await pilot.pause()


async def _drive_banner_degraded(app: DoxaApp, pilot) -> None:
    await _settle(pilot)
    # Same reasoning as _drive_banner just above.
    await app.action_new_tab()
    await pilot.pause()
    await app.action_new_tab()
    await pilot.pause()
    tabbed = app.query_one("#session-tabs")
    tabbed.active = app.panes()[0].id or tabbed.active
    await pilot.pause()


async def _drive_image_support(app: DoxaApp, pilot) -> None:
    await _settle(pilot)
    await app.active_pane._cmd_img("")
    await _settle(pilot)


# --------------------------------------------------------------------- #
# Scene: beliefs-browser -- item V's full-height tab, not the ten-row
# picker (already shown in `memory`): scope-grouped beliefs carrying
# LORE's own outcome verbs (confirmed/contradicted/never tested), and the
# staged-proposals half beneath them with one row armed mid-approve, so
# the two-step "arm, then confirm" control reads as itself rather than as
# an ordinary button.
# --------------------------------------------------------------------- #

_DAY = 86400.0


def _stamp(secs_ago: float) -> str:
    from time import gmtime, strftime, time as now

    return strftime("%Y-%m-%dT%H:%M:%SZ", gmtime(now() - secs_ago))


def _belief(bid, subject, claim, *, outcome=None, outcome_days=2,
            confidence=0.91, evidence_count=4) -> dict:
    belief = {
        "id": bid, "subject": subject, "claim": claim,
        "confidence": confidence,
        "created": _stamp(120 * _DAY), "updated": _stamp(9 * _DAY),
        "last_referenced": _stamp(9 * _DAY),
        "via": "derived", "evidence_count": evidence_count,
        "outcomes": 1 if outcome else 0,
    }
    if outcome:
        belief.update({
            "outcome_event": outcome, "outcome_at": _stamp(outcome_days * _DAY),
            "outcome_source": "dream", f"outcome_{outcome}s": 1,
        })
    return belief


def _proposal(pid, text, *, kind="memory", scope="project") -> dict:
    return {
        "pid": pid, "kind": kind, "action": "add", "scope": scope,
        "text": text, "created": _stamp(3 * _DAY),
        "session_id": "a1b2c3d4e5", "project": "doxa",
    }


def _live_lore_write_state() -> dict:
    """FakeEngine's own `lore_write_state_result` default hard-codes
    ``"version": "0.36.0"`` -- fine for a unit test, which scripts a
    capability and does not care which real lore_core shipped it, but the
    beliefs-browser scene renders that string straight into its header
    (``lore_core {version} ({source})``), where it reads as a live fact
    about this checkout. Read the two real answers instead -- the same
    ones `doxa.version.lore_core_version()` and
    `doxa._lore_bootstrap.resolved_source()` give the real engine, per
    `test_write_state_reports_the_carrier_about_already_names` -- so the
    number moves when the pin in pyproject.toml does, rather than sitting
    fixed at whatever lore_core was current when this fixture was
    written."""
    from doxa import _lore_bootstrap, version as version_mod

    source = _lore_bootstrap.resolved_source()
    return {
        "capable": True,
        "version": version_mod.lore_core_version() or "unknown",
        "source": (source[0] if source else "unknown"),
        "location": (source[1] if source else ""),
        "reason": "",
    }


def _beliefs_engine() -> FakeEngine:
    """v0.67.0: this scene retired its own bespoke 134x36 for the shared
    WIDE geometry every scene now shares (see WIDE's own docstring) --
    which means the three beliefs and two proposals that fit THAT frame
    are no longer enough; a scrolling browser sized for a real store
    showing three rows reads as an empty product, not a full one. Scaled
    up to a plausible slice of an active repo's memory instead: still
    every claim genuine-sounding and every id fictional, same discipline
    the rest of this fake-identity gallery holds to throughout."""
    engine = FakeEngine([], model="claude-opus-4-5")
    engine.lore_write_state_result = _live_lore_write_state()
    engine.belief_action_state_result = {
        **engine.belief_action_state_result,
        "version": engine.lore_write_state_result["version"],
    }
    engine.list_beliefs_result = [
        _belief(184, "project:doxa",
                "deploy checklist checks the runbook before tagging",
                outcome="confirmed"),
        _belief(201, "project:doxa",
                "kg-stats batch job reuses the deploy gate", outcome=None),
        _belief(219, "project:doxa",
                "the daemon socket is per-worktree, not per-repo",
                outcome="confirmed", outcome_days=14),
        _belief(233, "project:doxa",
                "CI runs the full suite on every push to main",
                outcome=None),
        _belief(240, "project:doxa",
                "screenshot scenes must never spend real API credit",
                outcome="confirmed", outcome_days=40),
        _belief(255, "project:doxa",
                "the changelog is written in the concise house style",
                outcome=None),
        _belief(261, "project:doxa",
                "worktrees under doxa-worktrees/ are throwaway, never pushed to directly",
                outcome="stale", outcome_days=21),
        _belief(77, "user", "prefers terse commit messages",
                outcome="contradicted", outcome_days=6),
        _belief(91, "user", "reviews diffs before approving a merge",
                outcome="confirmed", outcome_days=2),
        _belief(103, "user", "wants AGPL license headers on every source file",
                outcome="confirmed", outcome_days=30),
        _belief(118, "user", "asks for the SPDX line first when a file is new",
                outcome=None),
        _belief(52, "user-model",
                "answers in the house voice: plain, no filler openers",
                outcome="confirmed", outcome_days=1),
        _belief(64, "user-model",
                "over-hedges when a claim is actually well-supported",
                outcome="contradicted", outcome_days=11),
        _belief(70, "user-model",
                "runs the full test suite before calling a change done",
                outcome=None),
    ]
    engine.list_pending_result = [
        _proposal("20260825-00", "remember uv, not pip, for this repo"),
        _proposal("20260825-01",
                   "belief #201 is superseded by a stricter version",
                   kind="belief"),
        _proposal("20260825-02",
                   "the gallery's geometry constants live in scripts/screenshot.py"),
        _proposal("20260825-03",
                   "belief #261 is stale -- worktrees now get pruned weekly",
                   kind="belief"),
        _proposal("20260825-04",
                   "record_gif.py shares scripts/screenshot.py's fake-identity helpers",
                   scope="project"),
        _proposal("20260825-05",
                   "user prefers PICKER-style fixed columns over ad hoc joins",
                   scope="user"),
    ]
    return engine


async def _drive_beliefs_browser(app: DoxaApp, pilot) -> None:
    from doxa.ui.beliefs import ProposalRow

    await pilot.pause()
    pane = app.active_pane
    await pane.open_beliefs_browser()
    tab = None
    for _ in range(200):
        tab = pane._beliefs_tab
        if tab is not None and tab.rows:
            break
        await pilot.pause(0.02)
    await pilot.pause(0.2)
    # Arm the first proposal's approve control -- the second click that
    # would actually apply it never happens, which is the point: the shot
    # is the "you have to mean it" state, not the aftermath.
    rows = [r for r in tab.rows if isinstance(r, ProposalRow)]
    rows[0].action_approve()
    await pilot.pause()


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
    Scene("beliefs-browser", _drive_beliefs_browser, size=WIDE,
          engine_factory=_beliefs_engine),
    Scene("error-block", _drive_error_block, size=WIDE,
          engine_factory=_hero_engine, new_session_factory=_sibling_tab_factory()),
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
        app = DoxaApp(
            cwd=str(ROOT),
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

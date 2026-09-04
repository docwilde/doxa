# SPDX-License-Identifier: AGPL-3.0-only
"""Record the README's animated demos -- one step further than
scripts/screenshot.py's single stills: each scene here drives the same
Textual Pilot + FakeEngine harness through a SEQUENCE of interaction
steps, snapshotting an SVG frame after every step, rasterizing each frame
to PNG with the same `inkscape --export-type=png` scripts/screenshot.py's
own gallery relies on, then assembling the sequence into a looping GIF
with Pillow (already on disk transitively via textual-image).

    uv run python scripts/record_gif.py             # every scene
    uv run python scripts/record_gif.py rename search  # just these

Deterministic by construction: every frame boundary follows an explicit
state change this script itself drove (a scripted FakeEngine event, a
direct reactive-attribute poke, a real key/click event) -- nothing here
waits out a fixed wall-clock delay hoping content has "settled" by then.
The small `pilot.pause(0.02)`-style polling loops that appear (borrowed
from tests/test_tab_status.py's own `_wait`) bridge asyncio scheduling
around a state transition THIS script already guaranteed will happen
(releasing a gated FakeEngine, submitting a key press) -- never a bet on
timing content into existence.

Geometry: reuses scripts/screenshot.py's calibrated constants -- width =
12.2*cols + 18, height = 24.375*rows + 51 -- and its own already-vetted
terminal sizes (each one individually solved for 16:9 within ~2%), so a
scene here just reuses whichever of those sizes fits the content instead
of re-deriving a new one.

Size discipline: every frame in a scene is palette-quantized to ONE
shared adaptive palette (built off a strip of every frame stacked
together, not just the first -- a color that only appears once a tab
flips to amber or green must still make the cut) before Pillow writes the
looping GIF, keeping each file's palette table paid for once rather than
per frame. Byte size is reported per GIF; scripts/record_gif.py raises
rather than silently shipping a file that blew past the ~1MB target.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Awaitable, Callable
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# v0.67.0: isolation, BEFORE any doxa import -- see scripts/screenshot.py's
# own copy of this block for the measured defect it closes (`N proposals`/
# `mem u%p%` reading this machine's real, ambient lore_core state, neither
# scripted anywhere in this file either) and why this is `setdefault`, not
# a bare assignment (tests/test_record_gif.py imports this module under
# pytest, where conftest.py has already established its OWN isolated
# directory -- this must never clobber that).
_tmp = Path(tempfile.mkdtemp(prefix="doxa-gifs-"))
os.environ.setdefault("LORE_ROOT", str(_tmp / "lore"))
os.environ.setdefault("LORE_PROJECTS_DIR", str(_tmp / "projects"))
os.environ.setdefault("DOXA_RUNTIME_DIR", str(_tmp / "runtime"))
os.environ.setdefault("DOXA_HOME", str(_tmp / "doxa-home"))
os.environ.setdefault("XDG_CONFIG_HOME", str(_tmp / "xdg"))
os.environ.setdefault("DOXA_SKIP_FIRST_RUN", "1")

from time import monotonic  # noqa: E402

from PIL import Image  # noqa: E402

from doxa.app import (  # noqa: E402
    ChipPicker,
    DoxaApp,
    NeedsInputPopup,
    PromptInput,
    ReasoningSection,
    SessionSearch,
    TabRename,
    ToolCallsSection,
    ToolChip,
    TurnBlock,
)
from doxa.engine import EngineEvent  # noqa: E402
from doxa.ui.split import PaneTab, SplitBox  # noqa: E402
from doxa.ui.transcript import SPINNER_FRAMES  # noqa: E402
from scripts.screenshot import _fake_identity, _peer, _settle  # noqa: E402
from tests.fakes import FakeEngine  # noqa: E402
from textual.containers import VerticalScroll  # noqa: E402
from textual.content import Content  # noqa: E402
from textual.widgets import TabbedContent  # noqa: E402

SHOTS = ROOT / "assets" / "shots"

# Fake session-search hits: same shape scripts/screenshot.py's own
# (now-retired) static search scene used, kept here since this is the
# only scene left that needs it -- no real title, timestamp, or snippet,
# same discipline as the rest of this fake-identity gallery.
SEARCH_HITS = [
    {"session_id": "f00dfeed01", "title": "deploy checklist rewrite",
     "ts": "2026-08-19T14:02:00",
     "snippet": "confirmed the [deploy] runbook now checks the [checklist] before tagging"},
    {"session_id": "cafebabe02", "title": "kg-stats refactor",
     "ts": "2026-08-17T09:41:00",
     "snippet": "reused the same [deploy] gate from the [checklist] item for the batch job"},
    {"session_id": "1234abcd03", "title": "onboarding notes",
     "ts": "2026-08-11T18:20:00",
     "snippet": "linked the release [checklist] from the team's [deploy] doc"},
]

# v0.67.0: every scene here shares scripts/screenshot.py's own `WIDE` --
# see that module's geometry comment for the full derivation (250 columns
# is the floor a live status bar needs, 69 rows solves 16:9 at that
# floor). The three named tiers this file used to pick between
# (SIZE_TAB_BAR/SIZE_WIDE/SIZE_SEARCH) are retired along with the mixed
# gallery they produced -- kept as ONE name below, still called out by
# scene below so a reader can see which scenes used to need which room.
SIZE_TAB_BAR = SIZE_WIDE = SIZE_SEARCH = WIDE = (250, 69)


# --------------------------------------------------------------------- #
# Frame capture + GIF assembly
# --------------------------------------------------------------------- #


def _svg_to_png(svg_path: Path, png_path: Path) -> None:
    subprocess.run(
        [
            "inkscape", "--export-type=png",
            f"--export-filename={png_path}", str(svg_path),
        ],
        check=True, capture_output=True,
    )


class FrameRecorder:
    """One instance per scene. `.snap()` captures the app's CURRENT
    rendered state as an SVG (Textual's own screenshot format); `.assemble`
    rasterizes every captured frame, quantizes the whole sequence to one
    shared palette, and writes the looping GIF."""

    def __init__(self, app: DoxaApp, work_dir: Path) -> None:
        self.app = app
        self.work_dir = work_dir
        self.frames: list[tuple[Path, int, str]] = []  # (svg, duration_ms, caption)
        self._n = 0

    def snap(self, duration_ms: int = 600, caption: str = "") -> None:
        self._n += 1
        svg_path = self.work_dir / f"frame_{self._n:03d}.svg"
        self.app.save_screenshot(str(svg_path))
        self.frames.append((svg_path, duration_ms, caption))

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    def assemble(self, out_path: Path) -> int:
        images = []
        for svg_path, _duration, _caption in self.frames:
            png_path = svg_path.with_suffix(".png")
            _svg_to_png(svg_path, png_path)
            images.append(Image.open(png_path).convert("RGB"))

        w, h = images[0].size
        for im in images:
            if im.size != (w, h):
                raise SystemExit(
                    f"frame size drifted mid-scene: {im.size} != {(w, h)} "
                    f"-- every frame in one scene must share one app size"
                )

        # One shared palette for the whole sequence: stack every frame
        # into a tall strip first so a color that only shows up once (the
        # amber/green tab dot, the palette's highlighted row) still lands
        # in the 256-color table, rather than only whatever the FIRST
        # frame happened to contain.
        strip = Image.new("RGB", (w, h * len(images)))
        for i, im in enumerate(images):
            strip.paste(im, (0, i * h))
        palette_img = strip.convert("P", palette=Image.ADAPTIVE, colors=256)

        quantized = [
            im.quantize(palette=palette_img, dither=Image.NONE) for im in images
        ]
        durations = [d for _, d, _ in self.frames]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        quantized[0].save(
            out_path, format="GIF", save_all=True,
            append_images=quantized[1:], duration=durations,
            loop=0, optimize=True, disposal=2,
        )
        return out_path.stat().st_size


async def _wait_until(pilot, cond: Callable[[], bool], tries: int = 150) -> bool:
    """Same shape as tests/test_tab_status.py's own `_wait`: poll a
    condition THIS script already guaranteed will become true (a gated
    FakeEngine released, a key press submitted) -- bridges asyncio
    scheduling, never a bet on timing."""
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


async def _mount_bare_turn(app: DoxaApp, prompt: str) -> TurnBlock:
    """Mount a TurnBlock directly, same helper shape as
    tests/test_restyle.py's `_mount_bare_turn` -- lets a scene drive
    add_tool_chip/append_text/mark_done with exact control over ordering,
    without waiting on an engine's own event timing."""
    assert app.active_pane is not None
    block_list = app.active_pane.query_one("#block-list", VerticalScroll)
    block = TurnBlock(prompt)
    await block_list.mount(block)
    return block


def _marker(block: TurnBlock, seconds: int, phase: str = "") -> None:
    """Paint the in-flight marker as a REAL turn's would read ``seconds``
    into it, in ``phase``.

    v0.87.0, and the fix for a gap CHANGELOG 0.78.0 named and left open.
    Every scene in this file mounts its TurnBlock directly
    (:func:`_mount_bare_turn`), which is what gives a scene exact control
    over ordering -- but it also means nothing here ever reached
    ``ThinkingMarker.start()``, the method a real turn calls from
    ``_run_turn`` and the two ``_peer_pump`` branches. So the three scenes
    that show a turn IN FLIGHT (`tool-calls`, `markdown-stream`,
    `reasoning`) baked the widget's un-armed CONSTRUCTION text, ``⋯
    thinking`` -- a state no real turn is ever in for even one frame, and
    exactly the pre-0.78.0 marker that release replaced with a per-second
    ``⠋ working (14s)`` ticker.

    Written by assignment rather than by arming the real one-second
    ``Timer``, and that is this file's own determinism rule (see the module
    docstring: "nothing here waits out a fixed wall-clock delay"), not a
    shortcut around it. A live timer would put a DIFFERENT elapsed count in
    the GIF on every run, which is precisely the non-reproducibility the
    frozen clock in ``scripts/screenshot.py``'s own `clock` scene exists to
    avoid. The three values written are the ones the widget computes for
    itself and are read back off it, not guessed: ``_elapsed()`` floors
    ``monotonic() - _started_at``, and ``_tick`` advances exactly one frame
    per second from zero -- so second N here renders the frame a real turn
    genuinely shows at second N. ``_repaint`` is the widget's own painter
    (it is deliberately not called ``_render``; see its comment).
    """
    marker = block.thinking
    marker._started_at = monotonic() - seconds
    marker.phase = phase
    marker.frame = seconds % len(SPINNER_FRAMES)
    marker._repaint()


async def _mount_filler_exchange(app: DoxaApp, pilot: Any) -> None:
    """One quick, already-FINISHED exchange, mounted before a scene's own
    scripted content -- v0.67.0's answer to every GIF scene here growing
    to the shared 250x69 (see scripts/screenshot.py's `WIDE`): most of
    these used to run at 120x32 or 172x47, sized for exactly the one
    interaction each demos, and simply widening/heightening that to the
    new floor would leave a tall blank scrollback above it. A prior turn
    is what a real session actually has by the time anything interesting
    happens in it, so this is the same discipline `hero`'s own three-tab
    conversation already established, sized down to one quick exchange
    rather than a whole scripted script -- every OTHER frame in the scene
    is still the thing the scene is actually about."""
    block = await _mount_bare_turn(
        app, "what's the shared frame size for the gallery now?")
    await block.append_text(
        "250 columns, 69 rows -- solved once for 16:9 at the widest floor "
        "any scene needs (a live status bar), and every scene shares it "
        "now instead of picking its own."
    )
    await block.mark_done(0.0014, 380, False)
    await pilot.pause()


def _show_search_hits(popup: SessionSearch, query: str, hits: list[dict]) -> None:
    """Paint scripted hits without ever touching the real session index --
    same trick scripts/screenshot.py's search scene uses (skip `sync()`'s
    debounce + real `search_sessions()` call, land on `_render` directly)
    -- but this scene stays open across SEVERAL more frames after this
    call, long enough for the debounce timer `sync()` already armed to
    fire for real, so it is explicitly disarmed here too: stopped, and any
    worker already launched for a stale query invalidated via `_seq`."""
    popup.display = True
    if popup._timer is not None:
        popup._timer.stop()
        popup._timer = None
    popup._seq += 1
    popup._render(query, hits)


# --------------------------------------------------------------------- #
# Scene: tab-lifecycle -- amber -working, background -done-unseen, clears
# --------------------------------------------------------------------- #


class GatedEngine(FakeEngine):
    """Same shape as tests/test_tab_status.py's own GatedEngine: a turn
    that holds open after turn_started until the scene releases it -- the
    only way to capture -working mid-turn deterministically, since a plain
    FakeEngine.send() runs its whole script without ever yielding control
    back."""

    def __init__(self, model: str = "claude-sonnet-4-5") -> None:
        super().__init__([], model=model)
        self.release = asyncio.Event()

    async def send(self, prompt: str):
        self.received_prompts.append(prompt)
        yield EngineEvent("turn_started", {})
        await self.release.wait()
        self.total_cost_usd += 0.0012
        yield EngineEvent(
            "turn_done", {"cost_usd": 0.0012, "duration_ms": 640, "is_error": False}
        )


async def _drive_tab_lifecycle(app: DoxaApp, pilot: Any, rec: FrameRecorder) -> None:
    await pilot.pause()
    first = app.active_pane
    await _mount_filler_exchange(app, pilot)
    rec.snap(1000, "one tab, idle")

    await app.action_new_tab()
    await pilot.pause()
    second = app.panes()[1]
    assert isinstance(second.engine, GatedEngine)

    second.query_one("#prompt-input", PromptInput).value = "any updates on the deploy gate?"
    await pilot.press("enter")
    assert await _wait_until(pilot, lambda: second.turn_in_flight)
    # `tabbed_holding`, not `query_one("#session-tabs")`: v0.97.0 gave
    # every pane GROUP a tab strip of its own, so "the strip" is a question
    # about which group. This scene has one group, but naming it by the tab
    # it holds is what keeps the next split-shaped scene honest.
    tabbed = app.tabbed_holding(second.tab_id)
    tab_second = tabbed.get_tab(second.tab_id)
    assert await _wait_until(pilot, lambda: tab_second.has_class("-working"))
    rec.snap(700, "tab 2 starts a turn: amber -working (active)")

    await pilot.press("ctrl+left")
    await pilot.pause()
    assert app.active_pane is first
    rec.snap(700, "switched to tab 1; tab 2 still -working in the background")

    second.engine.release.set()
    assert await _wait_until(pilot, lambda: not second.turn_in_flight)
    assert await _wait_until(pilot, lambda: tab_second.has_class("-done-unseen"))
    rec.snap(700, "background turn finished: tab 2 shows green -done-unseen")

    await pilot.press("ctrl+right")
    await pilot.pause()
    assert app.active_pane is second
    assert not tab_second.has_class("-done-unseen")
    rec.snap(1000, "clicked back: the dot clears")


# --------------------------------------------------------------------- #
# Scene: tool-calls -- "Tool calls (N)" ticking live, then expand + expand
# --------------------------------------------------------------------- #


async def _drive_tool_calls(app: DoxaApp, pilot: Any, rec: FrameRecorder) -> None:
    await pilot.pause()
    await _mount_filler_exchange(app, pilot)
    block = await _mount_bare_turn(app, "what changed in the token refresh path?")
    await block.append_text("Checking a few places before I answer.\n\n")
    _marker(block, 2, "generating")
    await pilot.pause()
    rec.snap(700, "turn streams: prose before any tool call -- ⠹ generating (2s)")

    # The elapsed count climbing 5 -> 9 -> 14 across the next three frames
    # is v0.78.0's whole point, and it is the honest reading of this exact
    # sequence: between a tool_call and its tool_result NO delta arrives,
    # so a purely delta-driven marker (everything before 0.78.0) froze
    # here -- on the one stretch where "is this still working?" is the
    # question actually being asked. The phase legitimately stops changing
    # at `working`; the glyph and the seconds do not.
    chip1 = ToolChip("t1", "Grep", {"pattern": "refresh_token", "path": "doxa/auth.py"})
    await block.add_tool_chip(chip1)
    _marker(block, 5, "working")
    await pilot.pause()
    rec.snap(550, "Tool calls (1) -- ⠴ working (5s)")

    chip2 = ToolChip("t2", "Read", {"path": "doxa/auth.py"})
    await block.add_tool_chip(chip2)
    _marker(block, 9, "working")
    await pilot.pause()
    rec.snap(550, "Tool calls (2) -- ⠏ working (9s), ticking through dead air")

    chip3 = ToolChip("t3", "Edit", {"path": "doxa/auth.py"})
    await block.add_tool_chip(chip3)
    _marker(block, 14, "working")
    await pilot.pause()
    rec.snap(550, "Tool calls (3) -- ⠼ working (14s)")

    chip1.update_result("3 hits in doxa/auth.py", False, 12)
    chip2.update_result("214 lines read", False, 6)
    chip3.update_result("1 edit applied", False, 40)

    block.tool_section.collapsed = False
    _marker(block, 17, "working")
    await pilot.pause()
    rec.snap(700, "section expanded: all 3 chips")

    chip1.collapsed = False
    _marker(block, 19, "working")
    await pilot.pause()
    rec.snap(1000, "chip 1 expanded: ARGS + RESULT")

    # mark_done -> hide_thinking(): the marker goes, and the turn's cost
    # line lands. The LAST frame is what a reader is left looking at, so
    # it has to be the finished turn, not a spinner frozen mid-flight.
    await block.mark_done(0.0028, 640, False)
    await pilot.pause()
    rec.snap(1200, "turn_done: the marker clears, cost and duration land")


# --------------------------------------------------------------------- #
# Scene: markdown-stream -- prose, a table row by row, bold + inline code
# --------------------------------------------------------------------- #

MD_CHUNKS = [
    "Here's what the last trace looked like:\n\n",
    "| step | tool | ms |\n|------|------|----|\n",
    "| 1 | Grep | 12 |\n",
    "| 2 | Read | 8 |\n",
    "| 3 | Edit | 40 |\n\n",
    "**Total**: 60ms across three calls, all via `Task`.\n",
]


async def _drive_markdown_stream(app: DoxaApp, pilot: Any, rec: FrameRecorder) -> None:
    await pilot.pause()
    await _mount_filler_exchange(app, pilot)
    block = await _mount_bare_turn(app, "summarize the last trace")
    last = len(MD_CHUNKS) - 1
    for i, chunk in enumerate(MD_CHUNKS):
        await block.append_text(chunk)
        # A text_delta is exactly what puts the marker in `generating`
        # (ThinkingMarker.advance's own phase names), so the marker moves
        # with the stream here rather than through dead air -- two seconds
        # a chunk, the shape a real reply of this length streams at.
        _marker(block, 2 * i + 1, "generating")
        await pilot.pause()
        hold = 1000 if i in (0, last) else 550
        rec.snap(hold, f"chunk {i + 1}/{len(MD_CHUNKS)} -- generating ({2 * i + 1}s)")
    await block.mark_done(0.0019, 420, False)
    await pilot.pause()
    rec.snap(1200, "turn_done: the marker clears, the table stands finished")


# --------------------------------------------------------------------- #
# Scene: reasoning -- "Reasoning (N chars)" ticking live while collapsed,
# expanded to read it, then the response streams in below once thinking
# finishes (v0.25.0)
# --------------------------------------------------------------------- #

REASONING_CHUNKS = [
    "The user is asking about the token refresh path. Let me trace through "
    "how the credential gets renewed before it expires, since that's the "
    "part most likely to have regressed.\n\n",
    "Looking at doxa/auth.py, refresh_token() reads the cached expiry, and "
    "if it's within the grace window it calls the CLI's own refresh "
    "endpoint rather than trying to do this itself.",
]


async def _drive_reasoning(app: DoxaApp, pilot: Any, rec: FrameRecorder) -> None:
    await pilot.pause()
    await _mount_filler_exchange(app, pilot)
    block = await _mount_bare_turn(app, "what changed in the token refresh path?")
    # The opening phase, before ANY event has arrived, renders as
    # `thinking` -- and since v0.78.0 it already carries a live second
    # count, deliberately: that silent opening gap is the case the
    # reported freeze was worst in. v0.56.0 kept this frozen; 0.78.0
    # reversed it, and this frame is that reversal.
    _marker(block, 0, "")
    await pilot.pause()
    rec.snap(700, "turn starts: ⠋ thinking (0s) is the only sign of life")

    for i, chunk in enumerate(REASONING_CHUNKS):
        await block.append_reasoning(chunk)
        _marker(block, 4 * i + 3, "reasoning")
        await pilot.pause()
        rec.snap(700, f"Reasoning ({block.reasoning_section.chars} chars) -- "
                      f"collapsed, ticking live at {4 * i + 3}s")

    block.reasoning_section.collapsed = False
    _marker(block, 11, "reasoning")
    await pilot.pause()
    rec.snap(1100, "expanded mid-turn: stays open as more streams in")

    await block.append_reasoning(
        " That's the one place a stale cache could linger past the window."
    )
    _marker(block, 15, "reasoning")
    await pilot.pause()
    rec.snap(900, "still expanded, one more chunk arrived")

    await block.append_text(
        "The refresh path looks correct: `refresh_token()` renews inside "
        "the grace window via the CLI's own endpoint."
    )
    # v0.25.0 had the first reasoning_delta HIDE this marker; v0.78.0
    # reversed that too, so one marker lives for the whole turn and NAMES
    # the phase it is in. The switch reasoning -> generating is that
    # reversal's payoff, and it is a frame here rather than a claim.
    _marker(block, 19, "generating")
    await pilot.pause()
    rec.snap(1100, "reasoning done -- the phase flips to generating, answer streams in below")

    await block.mark_done(0.0021, 780, False)
    await pilot.pause()
    rec.snap(1200, "turn_done: the marker clears")


# --------------------------------------------------------------------- #
# Scene: rename -- real double-click, typing, Enter commits
# --------------------------------------------------------------------- #


async def _drive_rename(app: DoxaApp, pilot: Any, rec: FrameRecorder) -> None:
    await pilot.pause()
    await app.action_new_tab()
    await pilot.pause()
    await app.action_new_tab()
    await pilot.pause()
    pane = app.panes()[1]
    tabbed_early = app.tabbed_holding(pane.tab_id)
    tabbed_early.active = pane.tab_id or tabbed_early.active
    # ...and say where the KEYBOARD goes, never just which tab shows.
    # v0.38.0's rule, and the same one-line omission that livelocked
    # scripts/screenshot.py's `_activate` (fixed in 2dac09b, whose
    # docstring traces the loop event by event): a raw `active` write
    # leaves the keyboard inside a TabPane that has just been hidden,
    # Textual re-homes focus INTO that hidden pane, focusing a widget
    # there re-activates it, and the two writers then fight forever at
    # ~90 focus moves per 40 idle pump turns. The app never goes idle,
    # so every later `_wait_until` in this scene races a busy pump --
    # which is how the real double-click below came to wait out its
    # budget for a `#tab-rename` that was never going to be reached.
    app._focus_tab(pane)
    await pilot.pause()
    await _mount_filler_exchange(app, pilot)
    rec.snap(900, "three tabs, before rename")

    tabbed = app.tabbed_holding(pane.tab_id)
    tab = tabbed.get_tab(pane.tab_id)
    # A REAL double-click, same event-chain path a mouse would take (see
    # doxa.app.DoxaApp._on_click_maybe_rename, event.chain == 2) --
    # scripts/screenshot.py's static rename shot called `_start_rename`
    # directly instead; this scene exercises the actual trigger.
    await pilot.click(tab, times=2)
    assert await _wait_until(pilot, lambda: bool(app.query("#tab-rename")))
    editor = app.query_one("#tab-rename", TabRename)
    rec.snap(600, "double-click opens the inline editor")

    editor.value = "kg-stats"
    editor.cursor_position = len(editor.value)
    await pilot.pause()
    rec.snap(500, "typing the new name")

    editor.value = "kg-stats refi"
    editor.cursor_position = len(editor.value)
    await pilot.pause()
    rec.snap(600, "new name typed, about to commit")

    await pilot.press("enter")
    await pilot.pause()
    rec.snap(1000, "Enter commits: tab renamed")


# --------------------------------------------------------------------- #
# Scene: palette -- Ctrl+P, arrow through entries, Esc
# --------------------------------------------------------------------- #


async def _drive_palette(app: DoxaApp, pilot: Any, rec: FrameRecorder) -> None:
    await pilot.pause()
    await app.action_new_tab()
    await pilot.pause()
    # The palette is one of the dimmed-wash modals (CommandPalette in
    # theme.tcss's own list) -- the ACTIVE tab shows through it, so the
    # filler goes on THIS (the new, now-active) tab rather than the one
    # left behind.
    await _mount_filler_exchange(app, pilot)
    daemons = [_peer("9988aabb04", "midnight repro session", clients=0)]
    with mock.patch("doxa.peers.list_daemons", return_value=daemons):
        await pilot.press("ctrl+p")
        await _settle(pilot, 15)
        rec.snap(900, "Ctrl+P opens the palette")

        for _ in range(2):
            await pilot.press("down")
            await pilot.pause()
        rec.snap(600, "arrowed down")

        for _ in range(3):
            await pilot.press("down")
            await pilot.pause()
        rec.snap(600, "arrowed down further")

        await pilot.press("escape")
        await pilot.pause()
        rec.snap(900, "Esc closes the palette")


# --------------------------------------------------------------------- #
# Scene: search -- /search query, the tree (item I), excerpt insertion
# (item J) -- both fit in one recording, so this is the only GIF item I/J
# needs (search.gif; see CHANGELOG.md's 0.21.0 entry).
# --------------------------------------------------------------------- #


async def _drive_search(app: DoxaApp, pilot: Any, rec: FrameRecorder) -> None:
    # Belt and braces on top of _show_search_hits's own timer-disarming:
    # even if the debounce timer somehow fired anyway, it would hit these
    # patched stand-ins, never the real on-disk session index.
    with mock.patch("doxa.history.search_sessions", return_value=[]), \
         mock.patch("doxa.history.recent_sessions", return_value=[]):
        await pilot.pause()
        await _mount_filler_exchange(app, pilot)
        # A second, scene-specific exchange -- the search popup anchors
        # directly above the prompt regardless of scroll content, so the
        # one filler every other scene uses left a tall gap between it
        # and the popup here. A real prior turn about the very thing
        # being searched for is honest filler, not padding for its own
        # sake.
        block = await _mount_bare_turn(app, "has deploy checklist history come up before?")
        await block.append_text(
            "Yes -- three sessions touch it: a rewrite, the kg-stats "
            "refactor reusing its gate, and the onboarding notes linking "
            "it. `/search deploy checklist` finds all three."
        )
        await block.mark_done(0.0011, 310, False)
        await pilot.pause()
        prompt = app.query_one("#prompt-input", PromptInput)
        popup = app.query_one("#session-search", SessionSearch)

        prompt.value = "/search deploy"
        await pilot.pause()
        _show_search_hits(popup, "deploy", SEARCH_HITS[:1])
        await pilot.pause()
        rec.snap(700, "typing '/search deploy': one session, flat")

        prompt.value = "/search deploy checklist"
        await pilot.pause()
        _show_search_hits(popup, "deploy checklist", SEARCH_HITS)
        await pilot.pause()
        rec.snap(800, "full query: 3 sessions -- item I groups into headers")

        popup.move(1)
        await pilot.pause()
        rec.snap(600, "arrow down: second session header selected")

        popup.expand_current()
        await pilot.pause()
        rec.snap(800, "right arrow expands it: its snippet appears")

        popup.move(1)
        await pilot.pause()
        rec.snap(600, "arrow down onto the snippet, matches still highlighted")

        await pilot.press("enter")
        await pilot.pause()
        rec.snap(1200, "item J: enter inserts the excerpt -- provenance line, then the snippet")


# --------------------------------------------------------------------- #
# Scene (optional, cheap): attention-blink -- 4 alternating frames
# --------------------------------------------------------------------- #


async def _drive_attention_blink(app: DoxaApp, pilot: Any, rec: FrameRecorder) -> None:
    await pilot.pause()
    await _mount_filler_exchange(app, pilot)
    pane = app.active_pane
    assert pane is not None
    pane.set_needs_input(True)
    await pilot.pause()
    rec.snap(500, "needs_input: starts off")

    pane._blink_attention()
    await pilot.pause()
    rec.snap(500, "blink on: -attention (red)")

    pane._blink_attention()
    await pilot.pause()
    rec.snap(500, "blink off")

    pane._blink_attention()
    await pilot.pause()
    rec.snap(500, "blink on again")

    pane.set_needs_input(False)


# --------------------------------------------------------------------- #
# Scene: needs-input -- queue item 5's AskUserQuestion dialog
# --------------------------------------------------------------------- #


async def _drive_needs_input(app: DoxaApp, pilot: Any, rec: FrameRecorder) -> None:
    """Same fake-event injection _drive_attention_blink already uses
    (push_peer_event, the out-of-band queue every FakeEngine exposes) --
    but pushing a REAL needs_input payload this time, so the dialog it
    drives is the actual doxa.app.NeedsInputPopup, not just the bare
    tab-blink mechanism the attention-blink scene demos on its own."""
    await pilot.pause()
    await _mount_filler_exchange(app, pilot)
    pane = app.active_pane
    assert pane is not None
    pane.engine.push_peer_event(EngineEvent("needs_input", {
        "id": "demo-1", "kind": "ask_user", "tool_name": "AskUserQuestion",
        "questions": [{
            "question": "Which environment should the migration run against?",
            "header": "Target environment",
            "options": [
                {"label": "staging", "description": "safe to re-run"},
                {"label": "production", "description": "one-way -- needs the maintenance window"},
            ],
            "multiSelect": False,
        }],
    }))
    popup = pane.query_one("#needs-input-popup", NeedsInputPopup)
    for _ in range(50):
        if popup.is_open:
            break
        await pilot.pause(0.02)
    await pilot.pause()
    rec.snap(1200, "AskUserQuestion: the tab blinks, the dialog opens above the prompt")

    await pilot.press("down")
    await pilot.pause()
    rec.snap(700, "arrow down highlights 'production'")

    await pilot.press("enter")
    await pilot.pause()
    rec.snap(1000, "Enter answers it -- dialog clears, blink stops")


# --------------------------------------------------------------------- #
# Scene: chip-picker -- status-chips (item Y): a real click on the branch
# chip opens the shared dropdown, typing narrows it, Enter switches
# --------------------------------------------------------------------- #


def _status_plain(app: DoxaApp) -> str:
    bar = app.query_one("#status-bar")
    return Content.from_markup(str(bar.renderable)).plain


async def _drive_chip_picker(app: DoxaApp, pilot: Any, rec: FrameRecorder) -> None:
    """The status bar stopped being inert text (the operator's own report):
    the branch chip opens the SAME shared :class:`ChipPicker` the model
    chip does. The status bar's OWN branch text still comes from the REAL
    git repo this script runs in (via the real ``GitLine`` -- unscripted,
    same as every other scene here); only the picker's CANDIDATE list is
    scripted on the FakeEngine (``switch_branch``), same separation
    tests/test_status_chips.py's branch tests already draw, so the demo
    branch names are legible regardless of which real branch this
    checkout happens to be on."""
    await pilot.pause()
    pane = app.active_pane
    assert pane is not None
    assert await _wait_until(pilot, lambda: pane._git is not None)
    assert await _wait_until(pilot, lambda: "⎇" in _status_plain(app))
    await _mount_filler_exchange(app, pilot)
    fake = pane.engine
    fake.branch_list_result = {
        "branches": ["main", "feature/observability", "hotfix/timeout"],
        "base": "main", "checked_out": "main",
    }
    fake.branch_switch_result = {
        "ok": True, "base": "feature/observability",
        "message": "doxa/f13526d4 now based on feature/observability",
    }
    rec.snap(900, "status bar before: the branch chip reads like ordinary text")

    # A REAL click, same "exercise the actual trigger" choice _drive_rename
    # makes with its double-click -- landing just past the "⎇" glyph is
    # enough (any x inside the branch span opens the SAME picker).
    plain = _status_plain(app)
    offset = (2 + plain.index("⎇") + 2, 0)
    await pilot.click("#status-bar", offset=offset)
    picker = pane.query_one("#chip-picker", ChipPicker)
    assert await _wait_until(pilot, lambda: picker.is_open)
    await pilot.pause()
    rec.snap(1100, "click opens the picker: branches listed, current one marked")

    await pilot.press("f", "e", "a")
    await pilot.pause()
    rec.snap(900, "typing 'fea' narrows to feature/observability")

    await pilot.press("enter")
    assert await _wait_until(pilot, lambda: not picker.is_open)
    assert await _wait_until(pilot, lambda: "feature/observability" in fake.branch_calls)
    await pilot.pause()
    rec.snap(1100, "Enter selects it -- the SAME /branch switch path, status bar updates")


# --------------------------------------------------------------------- #
# Scene: permission-mode -- v0.50.0's chip, FIRST on the row since it is
# the one chip that must never fall off a narrow terminal: default (grey)
# -> plan (teal, cycle-safe) -> auto (amber, a model classifier approves
# instead of you) -> bypassPermissions (red, nothing left to decline). The
# engine is armed (bypass_armed=True) so the red tier is even reachable --
# see doxa.engine.available_modes: an unarmed session never lists it at
# all, which is v0.58.0's whole point and not something a demo should
# paper over by faking a wider ring than a real unarmed session gets.
# --------------------------------------------------------------------- #


async def _drive_permission_mode(app: DoxaApp, pilot: Any, rec: FrameRecorder) -> None:
    await pilot.pause()
    pane = app.active_pane
    assert pane is not None
    await _mount_filler_exchange(app, pilot)
    rec.snap(900, "default mode chip leads the row -- grey, painted even at rest")

    await pane.open_mode_picker()
    picker = pane.query_one(ChipPicker)
    assert await _wait_until(pilot, lambda: picker.is_open)
    await pilot.pause()
    rec.snap(1000, "the SAME picker every chip opens -- six modes, grouped by "
                   "how you reach them")

    await pilot.press("p", "l", "a", "n")
    await pilot.pause()
    rec.snap(700, "typing 'plan' narrows to one row")

    await pilot.press("enter")
    assert await _wait_until(pilot, lambda: pane.engine.permission_mode == "plan")
    await pilot.pause()
    rec.snap(900, "Enter switches it -- the chip turns teal")

    await pane.open_mode_picker()
    await pilot.press("a", "u", "t", "o")
    await pilot.press("enter")
    assert await _wait_until(pilot, lambda: pane.engine.permission_mode == "auto")
    await pilot.pause()
    rec.snap(1000, "auto: amber -- a model classifier approves each call instead of you")

    await pane.open_mode_picker()
    await pilot.press("b", "y", "p", "a", "s", "s")
    await pilot.press("enter")
    assert await _wait_until(
        pilot, lambda: pane.engine.permission_mode == "bypassPermissions"
    )
    await pilot.pause()
    rec.snap(1300, "bypassPermissions: red -- every call runs unapproved, "
                   "nothing left to decline")


# --------------------------------------------------------------------- #
# Scene: peers-picker -- the status bar's peers chip (v0.79.0) opening
# into the shared ChipPicker: one row per live peer carrying its own
# first-prompt title and running token total, and a note stating the
# heartbeat staleness bound. The one gallery item the 0.87.0 regeneration
# pass could NOT capture as a still -- the roster only exists once the
# chip is clicked -- so it gets a scene of its own instead.
# --------------------------------------------------------------------- #


def _demo_peers() -> list:
    """Three fabricated peers -- plausible in-repo work, not foo/bar --
    covering both fields a roster row shows (title, tokens) and the case
    doxa/peers.py's own ``PeerInfo.usage_tokens`` docstring names
    explicitly: a peer that has not completed a turn yet reports
    ``None``, and that must render as unknown, never as ``0 tok``.

    Built with :func:`scripts.screenshot._peer` (session id, title,
    clients -- the same helper `hero`/`sessions`/`palette` already use for
    a plausible, entirely fake registry entry) and then given a token
    total directly: ``_peer`` itself never takes one, since the ONE scene
    that needed a peer with unknown usage needs ``usage_tokens`` left at
    its dataclass default (``None``) rather than passed and overridden."""
    ingest = _peer(
        "b7e2f9a1c4", "trace the nightly ingest spool for zero-row folds",
        clients=1,
    )
    ingest.usage_tokens = 86_000
    auth = _peer(
        "d4c8e1b503", "chase the token refresh regression in doxa/auth.py",
        clients=0,
    )
    auth.usage_tokens = 142_000
    curation = _peer(
        "9a1f5c2e08", "review the v22 consolidation merge order before tagging",
        clients=1,
    )
    # usage_tokens left at PeerInfo's own default (None): this peer has not
    # finished a turn yet -- doxa.session.chips.PaneChipsMixin.
    # open_peers_picker renders that as fmt_tokens(None) == "tok --",
    # never "0 tok", and this is the row that puts that on screen.
    return [ingest, auth, curation]


async def _drive_peers_picker(app: DoxaApp, pilot: Any, rec: FrameRecorder) -> None:
    """A real click on the peers chip, same "exercise the actual trigger"
    choice `_drive_rename`'s double-click and `_drive_chip_picker`'s
    branch click already make -- `_offset_of`'s own convention from
    tests/test_status_chips.py (content starts at x=2 inside
    ``#status-bar``, theme.tcss's `padding: 0 2`).

    Peers come from the FakeEngine's own `peers=` kwarg (:func:`_demo_peers`,
    threaded through this scene's `engine_factory` below) -- the same
    surface `_hero_engine` already uses in scripts/screenshot.py -- rather
    than from `doxa.peers.read_registry`, which the module docstring's own
    isolation block never lets this script's own on-disk registry near in
    the first place."""
    await pilot.pause()
    pane = app.active_pane
    assert pane is not None
    await _mount_filler_exchange(app, pilot)
    # The exact rendered chip text, not a bare "peers" substring -- this
    # repo's own git identity (branch/repo-dir name) can legitimately
    # contain "peers" too (this scene's OWN worktree does:
    # "chore/peers-gif@peers-gif"), and a looser needle would land the
    # click on the git chip instead, opening the WRONG picker with zero
    # rows. "peers 3 (1⌁)" is the peers chip's own text -- three peers,
    # one of them (`auth`, clients=0 in :func:`_demo_peers`) detached.
    chip_text = "peers 3 (1⌁)"
    assert await _wait_until(pilot, lambda: chip_text in _status_plain(app))
    rec.snap(1000, f"status bar: '{chip_text}' -- three live peers on "
                   "this repo, one running detached")

    plain = _status_plain(app)
    offset = (2 + plain.index(chip_text), 0)
    await pilot.click("#status-bar", offset=offset)
    picker = pane.query_one("#chip-picker", ChipPicker)
    assert await _wait_until(pilot, lambda: picker.is_open)
    await pilot.pause()
    rec.snap(1400, "click opens the roster: each row is a peer's title "
                   "(its own first-prompt excerpt) and its running token "
                   "total; the note states the heartbeat staleness bound")

    for _ in range(2):
        await pilot.press("down")
        await pilot.pause()
    rec.snap(1100, "arrowed to the peer with no completed turn yet: "
                   "renders 'tok —', never '0 tok'")

    await pilot.press("escape")
    await pilot.pause()
    rec.snap(900, "Esc closes the picker")


# --------------------------------------------------------------------- #
# Scene: split-panes (v0.94.0, NEW -- the feature is v0.91.0) -- the one
# thing a still cannot show about a split: the SECOND PANE APPEARING.
# scripts/screenshot.py's own `split-panes` shot carries the settled
# state (two independent sessions, side by side, one tab); this carries
# the gesture, which is what the owner asked for -- Ctrl+N, and a pane
# arriving where there was one.
#
# Driven by the REAL key throughout (`pilot.press("ctrl+n")`,
# `ctrl+shift+left`, `alt+right`), the same "exercise the actual trigger"
# choice `_drive_rename`'s double-click and `_drive_chip_picker`'s branch
# click already make -- tests/test_split_panes.py calls
# `split_active_pane` directly, which proves the mechanism but not the
# binding, and a demo of a keystroke has to press the keystroke.
#
# The key changed under this scene in v0.95.0. `pilot.press` reaches
# binding resolution directly, so it never saw what a real terminal saw:
# `alt+d` was undeliverable anywhere without the kitty protocol, because
# Textual decodes an ESC prefix as Escape-then-letter and not as Alt
# (doxa/keyboard.py). Recording the REAL key is worth less than it looks
# when the harness cannot reproduce the encoding -- hence Ctrl+N here and
# a parser-level assertion in tests/test_split_keys.py that pilot cannot
# fake. `alt+right` on the divider is untouched: a modified ARROW is
# CSI 1;3<final> and does decode.
# --------------------------------------------------------------------- #


async def _drive_split_panes(app: DoxaApp, pilot: Any, rec: FrameRecorder) -> None:
    await pilot.pause()
    left = app.active_pane
    assert left is not None
    await _mount_filler_exchange(app, pilot)
    rec.snap(1200, "one session, one pane -- the tab it is in could hold more")

    await pilot.press("ctrl+n")
    assert await _wait_until(pilot, lambda: app.active_pane is not left)
    right = app.active_pane
    assert right is not None
    assert await _wait_until(pilot, lambda: right.region.width > 0)
    assert right.region.x >= left.region.x + left.region.width
    await pilot.pause()
    rec.snap(1400, "ctrl+n: a SECOND SESSION lands beside it -- its own "
                   "transcript, its own status bar, and the keyboard")

    # `_mount_bare_turn` mounts into `app.active_pane`, which ctrl+n just
    # moved to the NEW pane -- that is where this turn belongs.
    assert app.active_pane is right
    block = await _mount_bare_turn(app, "who is in this tab now?")
    await block.append_text(
        "Two independent sessions -- this pane and the one to its left. "
        "A split spawns a new session through the same factory Ctrl+T "
        "uses; it is not a second view of the session you were in."
    )
    await block.mark_done(0.0016, 410, False)
    await pilot.pause()
    rec.snap(1400, "the new pane is a session in its own right, not a view")

    await pilot.press("ctrl+shift+left")
    assert await _wait_until(pilot, lambda: app.active_pane is left)
    await pilot.pause()
    rec.snap(1100, "ctrl+shift+← moves the keyboard back -- directional, "
                   "never 'next pane'")

    await pilot.press("alt+right")
    await _settle(pilot, 8)
    rec.snap(1400, "alt+→ moves the divider: the left pane grows, both "
                   "keep rendering")


# --------------------------------------------------------------------- #

@dataclass
class Scene:
    name: str
    drive: Callable[[DoxaApp, Any, FrameRecorder], Awaitable[None]]
    size: tuple[int, int]
    min_frames: int
    # Real widget classes this scene exercises -- imported at module load,
    # so a rename/removal of one of these breaks THIS FILE'S OWN IMPORTS
    # (loud, at collection time) rather than silently going stale; the
    # registry test also checks each entry really is a class object, not
    # a typo'd string nobody would ever notice.
    widgets: tuple[type, ...] = field(default_factory=tuple)
    engine_factory: "Callable[[], Any] | None" = None
    new_session_factory: "Callable[[], Any] | None" = None


SCENES: list[Scene] = [
    Scene(
        "tab-lifecycle", _drive_tab_lifecycle, size=SIZE_TAB_BAR, min_frames=4,
        widgets=(TabbedContent,),
        engine_factory=lambda: FakeEngine([], model="claude-opus-4-5"),
        new_session_factory=lambda: GatedEngine(),
    ),
    Scene(
        "tool-calls", _drive_tool_calls, size=SIZE_WIDE, min_frames=5,
        widgets=(TurnBlock, ToolChip, ToolCallsSection),
        engine_factory=lambda: FakeEngine([], model="claude-opus-4-5"),
    ),
    Scene(
        "markdown-stream", _drive_markdown_stream, size=SIZE_WIDE, min_frames=5,
        widgets=(TurnBlock,),
        engine_factory=lambda: FakeEngine([], model="claude-opus-4-5"),
    ),
    Scene(
        "reasoning", _drive_reasoning, size=SIZE_WIDE, min_frames=5,
        widgets=(TurnBlock, ReasoningSection),
        engine_factory=lambda: FakeEngine([], model="claude-opus-4-5"),
    ),
    Scene(
        "rename", _drive_rename, size=SIZE_TAB_BAR, min_frames=4,
        widgets=(TabRename, TabbedContent),
        engine_factory=lambda: FakeEngine([], model="claude-opus-4-5"),
        new_session_factory=lambda: FakeEngine([], model="claude-opus-4-5"),
    ),
    Scene(
        "palette", _drive_palette, size=SIZE_TAB_BAR, min_frames=3,
        widgets=(TabbedContent,),
        engine_factory=lambda: FakeEngine([], model="claude-opus-4-5"),
        new_session_factory=lambda: FakeEngine([], model="claude-sonnet-4-5"),
    ),
    Scene(
        "search", _drive_search, size=SIZE_SEARCH, min_frames=6,
        widgets=(SessionSearch, PromptInput),
        engine_factory=lambda: FakeEngine([], model="claude-opus-4-5"),
    ),
    Scene(
        "attention-blink", _drive_attention_blink, size=SIZE_TAB_BAR, min_frames=4,
        widgets=(TabbedContent,),
        engine_factory=lambda: FakeEngine([], model="claude-opus-4-5"),
    ),
    Scene(
        "needs-input", _drive_needs_input, size=SIZE_WIDE, min_frames=3,
        widgets=(NeedsInputPopup, TabbedContent),
        engine_factory=lambda: FakeEngine([], model="claude-opus-4-5"),
    ),
    Scene(
        "chip-picker", _drive_chip_picker, size=SIZE_TAB_BAR, min_frames=4,
        widgets=(ChipPicker,),
        engine_factory=lambda: FakeEngine([], model="claude-opus-4-5"),
    ),
    Scene(
        "permission-mode", _drive_permission_mode, size=SIZE_TAB_BAR, min_frames=6,
        widgets=(ChipPicker,),
        engine_factory=lambda: FakeEngine(
            [], model="claude-opus-4-5", bypass_armed=True,
        ),
    ),
    Scene(
        "split-panes", _drive_split_panes, size=WIDE, min_frames=5,
        widgets=(PaneTab, SplitBox, TurnBlock),
        engine_factory=lambda: FakeEngine([], model="claude-opus-4-5"),
        new_session_factory=lambda: FakeEngine([], model="claude-sonnet-4-5"),
    ),
    Scene(
        "peers", _drive_peers_picker, size=SIZE_TAB_BAR, min_frames=4,
        widgets=(ChipPicker,),
        engine_factory=lambda: FakeEngine(
            [], model="claude-opus-4-5", peers=_demo_peers(),
        ),
    ),
]


async def _run_scene(scene: Scene) -> dict:
    app = DoxaApp(
        cwd=str(ROOT),
        engine_factory=scene.engine_factory,
        new_session_factory=scene.new_session_factory,
    )
    with TemporaryDirectory(prefix=f"doxa-gif-{scene.name}-") as tmp_dir, _fake_identity():
        async with app.run_test(size=scene.size) as pilot:
            rec = FrameRecorder(app, Path(tmp_dir))
            await scene.drive(app, pilot, rec)
            if rec.frame_count < scene.min_frames:
                raise SystemExit(
                    f"scene {scene.name!r} captured {rec.frame_count} frame(s), "
                    f"expected >= {scene.min_frames}"
                )
            SHOTS.mkdir(parents=True, exist_ok=True)
            out = SHOTS / f"{scene.name}.gif"
            nbytes = rec.assemble(out)
    with Image.open(out) as im:
        width, height = im.size
    ratio = width / height
    print(
        f"saved {out}  {width}x{height} ({ratio:.3f}, target 1.778)  "
        f"{rec.frame_count} frames  {nbytes / 1024:.0f} KiB"
    )
    if nbytes > 1_000_000:
        print(f"  ! {out.name} is {nbytes / 1024:.0f} KiB -- over the 1MB target")
    return {"name": scene.name, "path": out, "frames": rec.frame_count, "bytes": nbytes}


async def main(names: list[str]) -> None:
    wanted = set(names) or {scene.name for scene in SCENES}
    unknown = wanted - {scene.name for scene in SCENES}
    if unknown:
        raise SystemExit(f"unknown scene(s): {', '.join(sorted(unknown))}")
    results = [await _run_scene(s) for s in SCENES if s.name in wanted]
    total = sum(r["bytes"] for r in results)
    print(f"total: {total / 1024:.0f} KiB across {len(results)} gif(s)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))

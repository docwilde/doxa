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
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Awaitable, Callable
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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

# The same 16:9-within-~2% terminal sizes scripts/screenshot.py already
# solved for its own scenes -- reused here rather than re-derived, since
# every frame in a scene shares one app size throughout.
SIZE_TAB_BAR = (120, 32)   # rename / palette / tab-lifecycle / attention-blink
SIZE_WIDE = (172, 47)      # tool-calls / markdown-stream: room for a turn body
SIZE_SEARCH = (100, 26)    # search: matches screenshot.py's own search scene


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
    rec.snap(1000, "one tab, idle")

    await app.action_new_tab()
    await pilot.pause()
    second = app.panes()[1]
    assert isinstance(second.engine, GatedEngine)

    second.query_one("#prompt-input", PromptInput).value = "any updates on the deploy gate?"
    await pilot.press("enter")
    assert await _wait_until(pilot, lambda: second.turn_in_flight)
    tabbed = app.query_one("#session-tabs", TabbedContent)
    tab_second = tabbed.get_tab(second.id or "")
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
    block = await _mount_bare_turn(app, "what changed in the token refresh path?")
    await block.append_text("Checking a few places before I answer.\n\n")
    await pilot.pause()
    rec.snap(700, "turn streams: prose before any tool call")

    chip1 = ToolChip("t1", "Grep", {"pattern": "refresh_token", "path": "doxa/auth.py"})
    await block.add_tool_chip(chip1)
    await pilot.pause()
    rec.snap(550, "Tool calls (1)")

    chip2 = ToolChip("t2", "Read", {"path": "doxa/auth.py"})
    await block.add_tool_chip(chip2)
    await pilot.pause()
    rec.snap(550, "Tool calls (2)")

    chip3 = ToolChip("t3", "Edit", {"path": "doxa/auth.py"})
    await block.add_tool_chip(chip3)
    await pilot.pause()
    rec.snap(550, "Tool calls (3)")

    chip1.update_result("3 hits in doxa/auth.py", False, 12)
    chip2.update_result("214 lines read", False, 6)
    chip3.update_result("1 edit applied", False, 40)

    block.tool_section.collapsed = False
    await pilot.pause()
    rec.snap(700, "section expanded: all 3 chips")

    chip1.collapsed = False
    await pilot.pause()
    rec.snap(1000, "chip 1 expanded: ARGS + RESULT")

    await block.mark_done(0.0028, 640, False)


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
    block = await _mount_bare_turn(app, "summarize the last trace")
    last = len(MD_CHUNKS) - 1
    for i, chunk in enumerate(MD_CHUNKS):
        await block.append_text(chunk)
        await pilot.pause()
        hold = 1000 if i in (0, last) else 550
        rec.snap(hold, f"chunk {i + 1}/{len(MD_CHUNKS)}")
    await block.mark_done(0.0019, 420, False)


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
    block = await _mount_bare_turn(app, "what changed in the token refresh path?")
    await pilot.pause()
    rec.snap(700, "turn starts: the ⋯ thinking marker is the only sign of life")

    for i, chunk in enumerate(REASONING_CHUNKS):
        await block.append_reasoning(chunk)
        await pilot.pause()
        rec.snap(700, f"Reasoning ({block.reasoning_section.chars} chars) -- collapsed, ticking live")

    block.reasoning_section.collapsed = False
    await pilot.pause()
    rec.snap(1100, "expanded mid-turn: stays open as more streams in")

    await block.append_reasoning(
        " That's the one place a stale cache could linger past the window."
    )
    await pilot.pause()
    rec.snap(900, "still expanded, one more chunk arrived")

    await block.append_text(
        "The refresh path looks correct: `refresh_token()` renews inside "
        "the grace window via the CLI's own endpoint."
    )
    await pilot.pause()
    rec.snap(1100, "reasoning done, the answer streams in below it")

    await block.mark_done(0.0021, 780, False)


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
    rec.snap(900, "three tabs, before rename")

    tabbed = app.query_one("#session-tabs", TabbedContent)
    tab = tabbed.get_tab(pane.id or "")
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

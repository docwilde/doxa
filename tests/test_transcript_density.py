# SPDX-License-Identifier: AGPL-3.0-only
"""v0.56.0: the tool-calls section condensed, and the in-flight marker
turned into a spinner that costs no timer.

Every assertion here is about what a user SEES -- a measured row count, a
rendered glyph, a visible label -- and that is the point rather than a
style preference. The v0.28.0 defect (widgets present in the DOM at zero
height, invisible for a full release) got through because the tests of the
day asserted structure: the widget existed, the class was applied, the
count was right. None of that can tell you whether anything was drawn. So
the density tests measure ``outer_size.height`` and read the rendered
strips back as text, and the spinner tests read the marker's rendered
content rather than checking that an attribute changed.

MEASURED BEFORE CUTTING, the way v0.44.0 measured the turn body. An
expanded three-call section cost 15 rows on v0.47.0:

    1   the "⚒ Tool calls (3)" header
    1   blank -- ToolCallsSection > Contents' top padding
    12  three chips x (border top, title, border bottom, margin blank)
    1   blank -- the section's own trailing margin-bottom

Four of those fifteen said anything. The other eleven are gone, and
:func:`test_an_expanded_three_call_section_costs_four_rows` is the pin.
"""

from __future__ import annotations

import pytest
from textual.containers import VerticalScroll

from doxa.app import (
    DoxaApp,
    SPINNER_FRAMES,
    ThinkingMarker,
    ToolCallsSection,
    ToolChip,
    TurnBlock,
)
from doxa.engine import EngineEvent
from tests.fakes import FakeEngine


BOX_GLYPHS = set("╭╮╰╯─│┌┐└┘━┃┏┓┗┛╔╗╚╝═║▊▉█")


def _rows(app, widget) -> "list[str]":
    """The rows of the real screen that ``widget`` occupies, as plain text.

    Composited, not self-rendered. ``widget.render_lines`` draws that one
    widget's own background, border and padding and NOTHING its children
    put on the screen -- which would make "is this row blank?" unanswerable
    for a container, and blank rows inside containers are half of what this
    release removed. The compositor is the only thing that knows what the
    terminal receives, so it is what gets asked."""
    strips = app.screen._compositor.render_strips()
    region = widget.region
    return [
        "".join(segment.text for segment in strips[y])[
            region.x : region.x + region.width
        ]
        for y in range(region.y, region.y + region.height)
    ]


async def _turn_with_tools(app, count: int = 3) -> TurnBlock:
    block_list = app.active_pane.query_one("#block-list", VerticalScroll)
    block = TurnBlock("hi")
    await block_list.mount(block)
    for index in range(count):
        chip = ToolChip(f"call-{index}", "Read", {"file_path": f"/tmp/f{index}"})
        await block.add_tool_chip(chip)
        chip.update_result("ok", False, 12)
    return block


def _app(tmp_path, monkeypatch, script=None) -> DoxaApp:
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: FakeEngine(list(script or [])),
    )
    return DoxaApp(cwd=str(tmp_path))


# -- 1. the condensed tool-calls section ------------------------------------


@pytest.mark.asyncio
async def test_an_expanded_three_call_section_costs_four_rows(monkeypatch, tmp_path):
    """The headline measurement: 15 rows became 4, and every one of the 4
    carries text. One header, one line per call, nothing else."""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        block = await _turn_with_tools(app)
        await pilot.pause()
        block.tool_section.collapsed = False
        await pilot.pause()
        await pilot.pause()

        # The whole tools AREA, margins included -- a trailing blank row
        # would show up here and nowhere else.
        assert block.tools.outer_size.height == 4
        assert block.tool_section.outer_size.height == 4
        for chip in block.tool_section.chips.children:
            assert chip.outer_size.height == 1


@pytest.mark.asyncio
async def test_the_section_draws_no_blank_row_anywhere(monkeypatch, tmp_path):
    """"Remove the empty line in between", asserted as the user meets it:
    every rendered row of an expanded section has ink on it."""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        block = await _turn_with_tools(app)
        await pilot.pause()
        block.tool_section.collapsed = False
        await pilot.pause()
        await pilot.pause()

        rows = _rows(app, block.tools)
        assert len(rows) == 4
        assert all(row.strip() for row in rows), rows


@pytest.mark.asyncio
async def test_no_chip_draws_a_box_around_itself(monkeypatch, tmp_path):
    """"Remove the boxes around". Read off the rendered strips rather than
    off the CSS: a border that survives as a default on some other rule
    still prints, and printing is what was complained about."""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        block = await _turn_with_tools(app)
        await pilot.pause()
        block.tool_section.collapsed = False
        await pilot.pause()
        await pilot.pause()

        painted = set("".join(_rows(app, block.tools)))
        assert not (painted & BOX_GLYPHS), sorted(painted & BOX_GLYPHS)


@pytest.mark.asyncio
async def test_every_chip_is_still_readable_after_the_cut(monkeypatch, tmp_path):
    """The v0.28.0 guard, restated for this change: condensing must not
    turn a chip into a present-but-invisible widget. Each call's own row
    is on screen, one row tall, naming the tool it ran."""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        block = await _turn_with_tools(app)
        await pilot.pause()
        block.tool_section.collapsed = False
        await pilot.pause()
        await pilot.pause()

        rows = _rows(app, block.tools)
        assert "Tool calls (3)" in rows[0]
        for index, row in enumerate(rows[1:]):
            assert "Read" in row, rows
            assert f"/tmp/f{index}" in row, rows


@pytest.mark.asyncio
async def test_chips_are_indented_under_their_header_not_separated_by_a_row(
    monkeypatch, tmp_path,
):
    """What replaced the boxes and the blank rows, and the reason a row was
    not the answer: indentation and the fold arrow cost nothing per chip,
    where a separator row is paid for once per call."""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        block = await _turn_with_tools(app)
        await pilot.pause()
        block.tool_section.collapsed = False
        await pilot.pause()
        await pilot.pause()

        rows = _rows(app, block.tools)
        header_indent = len(rows[0]) - len(rows[0].lstrip())
        for row in rows[1:]:
            assert len(row) - len(row.lstrip()) > header_indent, rows
            # Textual's own fold arrow leads every chip row -- the leading
            # glyph this release chose over a leading blank row.
            assert row.lstrip()[0] in "▶▼", row


@pytest.mark.asyncio
async def test_an_expanded_chip_spends_no_row_on_an_absent_subagent(
    monkeypatch, tmp_path,
):
    """An empty Static is still one row. Every expanded chip that never
    spawned a subagent -- i.e. nearly all of them -- was paying one."""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        block = await _turn_with_tools(app, count=1)
        await pilot.pause()
        block.tool_section.collapsed = False
        chip = list(block.tool_section.chips.children)[0]
        chip.collapsed = False
        await pilot.pause()
        await pilot.pause()

        assert chip._subout.display is False
        assert chip._subout.region.height == 0
        rows = _rows(app, chip)
        # The chip ENDS on ink, and holds exactly one blank row: the one
        # separating ARGS from RESULT. v0.44.0's rule restated -- a blank
        # row BETWEEN paragraphs is readability and stays; a blank row at
        # the END (which is what the absent subagent's empty Static was)
        # is waste and goes.
        assert rows[0].strip() and rows[-1].strip(), rows
        assert sum(1 for row in rows if not row.strip()) == 1, rows
        assert any("ARGS" in row for row in rows), rows
        assert any("RESULT" in row for row in rows), rows

        # ...and it comes back the moment there IS narration to show.
        chip.append_subagent_text("planning the edit")
        await pilot.pause()
        assert chip._subout.display is True
        assert any("planning the edit" in row for row in _rows(app, chip))


# -- 2. the spinner ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_marker_spins_and_says_generating_while_text_arrives(
    monkeypatch, tmp_path,
):
    """The half of the request about generating: while text_deltas land,
    the marker is on screen, carries a spinner glyph, and names the phase.
    """
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        block_list = app.active_pane.query_one("#block-list", VerticalScroll)
        block = TurnBlock("hi")
        await block_list.mount(block)
        await pilot.pause()

        await block.append_text("the answer begins")
        await pilot.pause()

        assert block.thinking.display is True
        shown = _rows(app, block.thinking)[0]
        assert "generating" in shown
        assert any(frame in shown for frame in SPINNER_FRAMES), shown


@pytest.mark.asyncio
async def test_the_marker_says_reasoning_while_the_model_thinks(monkeypatch, tmp_path):
    """The other half. v0.25.0 HID the marker here, on the grounds that a
    live "Reasoning (N chars)" header is itself a sign of life; v0.56.0
    reverses that, because one marker that survives the whole turn is what
    the request asked for and a collapsed fold whose count stopped moving
    reads exactly like a finished one."""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        block_list = app.active_pane.query_one("#block-list", VerticalScroll)
        block = TurnBlock("hi")
        await block_list.mount(block)
        await pilot.pause()

        await block.append_reasoning("weighing the options")
        await pilot.pause()

        assert block.thinking.display is True
        shown = _rows(app, block.thinking)[0]
        assert "reasoning" in shown
        assert any(frame in shown for frame in SPINNER_FRAMES), shown


@pytest.mark.asyncio
async def test_the_glyph_actually_moves_between_phases_and_deltas(
    monkeypatch, tmp_path,
):
    """A "spinner" that shows one frame forever is a static marker wearing
    a braille dot. Successive ticks must print different glyphs."""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        block_list = app.active_pane.query_one("#block-list", VerticalScroll)
        block = TurnBlock("hi")
        await block_list.mount(block)
        await pilot.pause()

        seen = []
        for phase in ("reasoning", "generating", "working", "generating"):
            block.thinking.advance(phase)
            await pilot.pause()
            seen.append(_rows(app, block.thinking)[0].strip()[0])
        assert len(set(seen)) > 1, seen


@pytest.mark.asyncio
async def test_the_marker_trails_the_output_it_is_waiting_on(monkeypatch, tmp_path):
    """A spinner nobody can see is not a spinner. The block list scrolls to
    the end after every event, so the marker has to be the LAST row of a
    running turn -- above a streaming answer it leaves the viewport inside
    a paragraph."""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        block_list = app.active_pane.query_one("#block-list", VerticalScroll)
        block = TurnBlock("hi")
        await block_list.mount(block)
        await pilot.pause()
        await block.append_text("an answer")
        await pilot.pause()
        await pilot.pause()

        marker_top = block.thinking.region.y
        assert marker_top > block.body.region.y
        assert marker_top >= block.tools.region.y


@pytest.mark.asyncio
async def test_the_spinner_is_gone_when_the_turn_is_done(monkeypatch, tmp_path):
    """Teardown, asserted as the pair it really is: the marker must have
    RUN (through reasoning, then generating) and then be gone. "Gone" on
    its own would pass on a marker that never showed."""
    script = [
        EngineEvent("turn_started", {}),
        EngineEvent("reasoning_delta", {"text": "hmm"}),
        EngineEvent("text_delta", {"text": "done thinking"}),
        EngineEvent("turn_done", {"cost_usd": 0.001, "duration_ms": 5}),
    ]
    app = _app(tmp_path, monkeypatch, script)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "go"
        await pilot.press("enter")
        for _ in range(200):
            blocks = list(app.query(TurnBlock))
            if blocks and "5ms" in str(blocks[0].title):
                break
            await pilot.pause(0.02)
        block = app.query_one(TurnBlock)
        # It ran: the last phase it reached was the answer streaming in.
        assert block.thinking.phase == "generating"
        # ...and then it left, taking its row with it.
        assert block.thinking.display is False
        assert block.thinking.region.height == 0
        assert block.thinking.auto_refresh is None


@pytest.mark.asyncio
async def test_the_spinner_is_gone_when_the_turn_fails(monkeypatch, tmp_path):
    """The error path (a refused turn, a dropped daemon connection) goes
    through the same mark_done, and a spinner left running on a turn that
    already broke is a lie the user has no way to clear."""

    class BrokenEngine(FakeEngine):
        async def send(self, prompt):
            yield EngineEvent("text_delta", {"text": "starting"})
            raise RuntimeError("the daemon went away")

    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda cwd, model=None: BrokenEngine([])
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "go"
        await pilot.press("enter")
        for _ in range(200):
            blocks = list(app.query(TurnBlock))
            if blocks and not blocks[0].thinking.display:
                break
            await pilot.pause(0.02)
        block = app.query_one(TurnBlock)
        # Same pairing as above: it was generating when the engine broke...
        assert block.thinking.phase == "generating"
        # ...and the broken turn still took it down.
        assert block.thinking.display is False
        assert block.thinking.region.height == 0


@pytest.mark.asyncio
async def test_a_late_delta_cannot_bring_the_spinner_back(monkeypatch, tmp_path):
    """Gone has to mean gone. The peer pump replays engine events, so a
    delta can be routed at this turn after its turn_done -- and a spinner
    reappearing on a turn that already printed its cost is worse than one
    that never showed.

    Driven at the marker rather than through ``append_text``: the response
    body's own stream refuses a write after ``mark_done`` stops it (that is
    v0.13.0's guarantee and it predates this), so the only tick that can
    actually reach a finished turn is the one this method takes."""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        block_list = app.active_pane.query_one("#block-list", VerticalScroll)
        block = TurnBlock("hi")
        await block_list.mount(block)
        await pilot.pause()
        await block.append_text("an answer")
        await block.mark_done(0.001, 10, False)
        await pilot.pause()

        block.thinking.advance("generating")
        block.thinking.advance("reasoning")
        await pilot.pause()

        assert block.thinking.display is False
        assert block.thinking.region.height == 0
        painted = set("".join(_rows(app, block)))
        assert not (painted & set(SPINNER_FRAMES)), sorted(painted & set(SPINNER_FRAMES))


@pytest.mark.asyncio
async def test_a_restored_turn_shows_no_spinner_at_all(monkeypatch, tmp_path):
    """Restore mounts the same widgets a live turn builds and closes each
    one with mark_done -- so scrollback must never come back with forty
    turns all claiming to be working. A restored turn never entered a
    phase at all, and nothing on screen carries a spinner glyph."""
    from doxa import transcript as transcript_mod
    from doxa.ui.transcript import mount_transcript

    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        block_list = app.active_pane.query_one("#block-list", VerticalScroll)
        snapshot = transcript_mod.Transcript(
            turns=[transcript_mod.Turn(prompt="old", text="old answer")],
        )
        await mount_transcript(block_list, snapshot)
        await pilot.pause()
        for block in app.query(TurnBlock):
            assert block.thinking.display is False
            assert block.thinking.phase == ""
        painted = set("".join(_rows(app, block_list)))
        assert not (painted & set(SPINNER_FRAMES)), sorted(painted & set(SPINNER_FRAMES))


@pytest.mark.asyncio
async def test_a_delta_burst_does_not_buy_a_repaint_per_delta(monkeypatch, tmp_path):
    """The cost discipline the no-timer rule exists to protect. The old
    LoadingIndicator repainted at a fixed 16 Hz; a spinner ticked by the
    delta stream would repaint at the MODEL's token rate, which is worse.
    SPINNER_MIN_INTERVAL floors it -- 500 deltas in one burst buy one
    frame, not 500."""
    marker = ThinkingMarker()
    marker.advance("generating")
    first = marker.frame
    for _ in range(500):
        marker.advance("generating")
    assert marker.frame == first, "the interval floor did not hold"

    # A PHASE CHANGE always gets through, floor or no floor: the switch
    # from reasoning to generating is the information, not the motion.
    marker.advance("reasoning")
    assert marker.frame != first
    assert marker.phase == "reasoning"


@pytest.mark.asyncio
async def test_the_tick_timer_lives_no_longer_than_the_turn_it_belongs_to(
    monkeypatch, tmp_path
):
    """v0.56.0's rule here used to be "delta-driven, not set_interval" --
    full stop, and this test's name used to say so. v0.78.0 amends it on
    purpose (see ThinkingMarker's own docstring for the full argument):
    an in-flight turn now arms exactly ONE per-second ``Timer``
    (``ThinkingMarker._tick_timer``), because a purely delta-driven
    spinner freezes for the whole length of a tool call, and that read as
    hung. The rule this test protects was never "no timer, ever" -- see
    :class:`doxa.ui.statusline.GitLine`'s own "no CPU spent when nothing
    is happening" -- it is that ONLY while a turn is genuinely in flight,
    and never a moment longer.

    So this test now checks the amended rule directly instead of a global
    absence:

    * ``_armed()`` -- an ``_auto_refresh_timer`` scan -- still returns
      ``[]`` throughout, because ``_tick_timer`` is a plain ``Timer``, not
      Textual's ``auto_refresh``. This mechanism is UNCHANGED by v0.78.0.
    * ``blocks[0].thinking.auto_refresh is None`` -- same reason, still
      true through a whole live turn.
    * ``blocks[0].thinking._tick_timer`` -- the actual new state -- is
      armed (not None) at some point while the marker is displayed, and
      is None again (not just hidden) once the turn ends. An idle app,
      sampled before the turn starts, arms none at all."""
    import asyncio as _asyncio

    from doxa.ui.statusline import ClockChip

    class ChattyEngine(FakeEngine):
        async def send(self, prompt):
            yield EngineEvent("turn_started", {})
            for _ in range(20):
                yield EngineEvent("text_delta", {"text": "word "})
                await _asyncio.sleep(0.01)
            yield EngineEvent("turn_done", {"cost_usd": 0.0, "duration_ms": 5})

    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda cwd, model=None: ChattyEngine([])
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        armed = lambda: [
            node for node in app.query("*")
            if not isinstance(node, ClockChip)
            and getattr(node, "_auto_refresh_timer", None) is not None
        ]
        # Idle, before the turn: no tick timer anywhere either.
        assert all(m._tick_timer is None for m in app.query(ThinkingMarker))

        app.query_one("#prompt-input").value = "go"
        await pilot.press("enter")

        spun = False
        tick_timer_seen_armed = False
        for _ in range(300):
            blocks = list(app.query(TurnBlock))
            if blocks:
                assert armed() == []
                assert blocks[0].thinking.auto_refresh is None
                if blocks[0].thinking.display and blocks[0].thinking._tick_timer is not None:
                    tick_timer_seen_armed = True
                if blocks[0].thinking.phase:
                    spun = True
                if not blocks[0].thinking.display and spun:
                    break
            await pilot.pause(0.02)
        assert spun, "the marker never advanced -- nothing was sampled"
        assert tick_timer_seen_armed, "the tick timer never armed during the turn"
        assert armed() == []
        # Turn done: the timer that WAS armed is gone, not just hidden --
        # sampled on every marker in the app, not only this turn's, so a
        # second finished turn cannot leave a second one behind either.
        assert all(m._tick_timer is None for m in app.query(ThinkingMarker))


@pytest.mark.asyncio
async def test_the_marker_ticks_once_a_second_with_no_events_at_all(
    monkeypatch, tmp_path,
):
    """The reported defect, reproduced directly: between two engine
    events (a slow tool call, a model silently thinking) the v0.56.0
    marker froze on whatever frame the last delta left it on. It must not
    any more -- and it must SHOW the wait rather than merely not-look-
    frozen, which is why this asserts the ELAPSED SECONDS incrementing,
    not just "the glyph changed" (a glyph that kept moving while the
    second count silently stalled would still pass a glyph-only check,
    and would still read as a lie about how long the wait has been).

    Real wall-clock waits, not a mocked clock -- the v0.28.0 rule this
    file already follows (assert rendered text, not query matches) and
    the bar this feature was built against (measure, don't assume):
    ``pilot.pause()`` actually advances the event loop the tick timer
    runs on, so a real ``Timer`` firing is what makes this pass, not an
    assumption that it would."""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        block_list = app.active_pane.query_one("#block-list", VerticalScroll)
        block = TurnBlock("hi")
        await block_list.mount(block)
        await pilot.pause()

        # What _run_turn does the instant a real turn begins -- no engine
        # event follows this in the whole test, on purpose.
        block.thinking.start()
        try:
            first = _rows(app, block.thinking)[0]
            assert "(0s)" in first, first

            await pilot.pause(1.2)
            second = _rows(app, block.thinking)[0]
            assert second != first, "the marker's text did not change across a tick"
            assert "(1s)" in second, second

            await pilot.pause(1.2)
            third = _rows(app, block.thinking)[0]
            assert third != second, "the marker's text did not change across a tick"
            assert "(2s)" in third, third
        finally:
            block.thinking.stop()


def test_the_section_still_declares_no_transition_and_no_indicator():
    """Belt and braces on the theme half: condensing must not have been
    paid for with an animation, and the spinner must not have quietly
    reintroduced Textual's animated widgets."""
    import doxa.app as app_mod

    for name in ("LoadingIndicator", "ProgressBar", "Sparkline"):
        assert not hasattr(app_mod, name), name
    assert isinstance(TurnBlock("hi").thinking, ThinkingMarker)
    assert issubclass(ToolCallsSection, __import__(
        "textual.widgets", fromlist=["Collapsible"]
    ).Collapsible)

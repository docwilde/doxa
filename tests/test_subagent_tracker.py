# SPDX-License-Identifier: AGPL-3.0-only
"""Subagent tracker (queue item 4): live view of Task-spawned subagents.

Covers the whole loop -- registry add/remove on the Task call's own
tool_call/tool_result lifecycle, the status-bar `⧉ N agents` chip
(hidden at 0, same convention as the peers chip), the second status row
(SubagentLine, mounted only while N>0), opening a read-only transcript
tab by clicking a running subagent's label (replay of what its Task chip
already buffered), live routing of further events into that open tab,
completion marking (✓ suffix + -done-unseen if not active), Ctrl+W
closing a transcript tab without treating it as a session, and the no-
provider-glyph rule for transcript tabs.

Same headless Pilot + FakeEngine pattern as tests/test_trace.py and
tests/test_tab_status.py; a GatedSubagentEngine (same shape as
test_tab_status.py's GatedEngine) holds the Task call open across two
checkpoints so a test can observe "still running" state deterministically
instead of racing a FakeEngine that replays its whole script in one go.
"""

from __future__ import annotations

import asyncio

import pytest

from doxa.app import (
    DoxaApp,
    SubagentLine,
    SubagentTranscriptTab,
    ToolChip,
)
from doxa.engine import EngineEvent
from textual.widgets import TabbedContent
from tests.fakes import FakeEngine


class GatedSubagentEngine(FakeEngine):
    """One turn: the main thread calls Task ("explore the auth module"),
    the subagent narrates and runs one Grep -- then the script PAUSES
    (open_gate) so a test can inspect "still running" state and open the
    transcript tab. Releasing open_gate delivers a SECOND nested call
    (Read) -- exercising live routing into an already-open tab -- then
    pauses again (finish_gate) before the Task's own tool_result and
    turn_done land."""

    def __init__(self, model: str = "claude-haiku-4-5") -> None:
        super().__init__([], model=model)
        self.open_gate = asyncio.Event()
        self.finish_gate = asyncio.Event()

    async def send(self, prompt: str):
        self.received_prompts.append(prompt)
        yield EngineEvent("turn_started", {})
        yield EngineEvent("tool_call", {
            "id": "task-1", "name": "Task",
            "input": {"description": "explore the auth module  ", "subagent_type": "Explore"},
        })
        yield EngineEvent("text_delta", {"text": "looking around", "parent_id": "task-1"})
        yield EngineEvent("tool_call", {
            "id": "sub-1", "name": "Grep", "input": {"pattern": "token"},
            "parent_id": "task-1",
        })
        yield EngineEvent("tool_result", {
            "id": "sub-1", "name": "Grep", "result_summary": "3 hits",
            "is_error": False, "duration_ms": 5, "parent_id": "task-1",
        })
        await self.open_gate.wait()
        yield EngineEvent("tool_call", {
            "id": "sub-2", "name": "Read", "input": {"path": "auth.py"},
            "parent_id": "task-1",
        })
        yield EngineEvent("text_delta", {"text": " more narration", "parent_id": "task-1"})
        yield EngineEvent("tool_result", {
            "id": "sub-2", "name": "Read", "result_summary": "200 lines",
            "is_error": False, "duration_ms": 8, "parent_id": "task-1",
        })
        await self.finish_gate.wait()
        yield EngineEvent("tool_result", {
            "id": "task-1", "name": "Task", "result_summary": "explored: 2 hits",
            "is_error": False, "duration_ms": 400,
        })
        self.total_cost_usd += 0.001
        yield EngineEvent("turn_done", {
            "cost_usd": 0.001, "duration_ms": 500, "is_error": False,
            "ctx_percentage": 5.0,
        })


async def _wait(pilot, cond, tries=150):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


def _tab(app, tab_id):
    return app.query_one("#session-tabs", TabbedContent).get_tab(tab_id)


async def _start_and_open(app, pilot):
    """Drive one turn up through the point where task-1 is registered,
    running, and its transcript tab is open (with sub-1 already replayed).
    Returns the pane and its open SubagentTranscriptTab."""
    pane = app.active_pane
    pane.query_one("#prompt-input").value = "explore the repo"
    await pilot.press("enter")
    assert await _wait(pilot, lambda: "task-1" in pane._subagents)

    line = pane.query_one("#subagent-line", SubagentLine)
    await pilot.click(line, offset=(2, 0))
    assert await _wait(pilot, lambda: "task-1" in pane._transcript_tabs)
    tab = pane._transcript_tabs["task-1"]
    return pane, tab


@pytest.mark.asyncio
async def test_registry_adds_on_task_call_and_removes_on_its_result(tmp_path):
    engine = GatedSubagentEngine()
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        pane.query_one("#prompt-input").value = "explore the repo"
        await pilot.press("enter")

        assert await _wait(pilot, lambda: "task-1" in pane._subagents)
        assert isinstance(pane._subagents["task-1"], ToolChip)

        engine.open_gate.set()
        engine.finish_gate.set()
        assert await _wait(pilot, lambda: not pane.turn_in_flight)
        assert "task-1" not in pane._subagents


@pytest.mark.asyncio
async def test_status_chip_hidden_at_zero_shown_at_n(tmp_path):
    engine = GatedSubagentEngine()
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        assert "⧉" not in str(pane.query_one("#status-bar").renderable)

        pane.query_one("#prompt-input").value = "explore the repo"
        await pilot.press("enter")
        assert await _wait(pilot, lambda: "task-1" in pane._subagents)
        assert await _wait(
            pilot,
            lambda: "⧉ 1 agent" in str(pane.query_one("#status-bar").renderable),
        )

        engine.open_gate.set()
        engine.finish_gate.set()
        assert await _wait(pilot, lambda: not pane.turn_in_flight)
        assert "⧉" not in str(pane.query_one("#status-bar").renderable)


@pytest.mark.asyncio
async def test_second_line_mounts_and_unmounts(tmp_path):
    engine = GatedSubagentEngine()
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        assert not pane.query("#subagent-line")
        assert pane._subagent_line is None

        pane.query_one("#prompt-input").value = "explore the repo"
        await pilot.press("enter")
        assert await _wait(pilot, lambda: bool(pane.query("#subagent-line")))
        line = pane.query_one("#subagent-line", SubagentLine)
        assert "explore the auth module" in str(line.renderable)

        engine.open_gate.set()
        engine.finish_gate.set()
        assert await _wait(pilot, lambda: not pane.turn_in_flight)
        # Unmounted, not merely hidden -- zero cost at idle.
        assert not pane.query("#subagent-line")
        assert pane._subagent_line is None


@pytest.mark.asyncio
async def test_click_opens_transcript_tab_with_replayed_content(tmp_path):
    engine = GatedSubagentEngine()
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane, tab = await _start_and_open(app, pilot)

        assert isinstance(tab, SubagentTranscriptTab)
        assert tab.base_label == "explore the auth module"
        assert "looking around" in tab._narration_text
        assert "sub-1" in tab.mirror_chips
        mirror = tab.mirror_chips["sub-1"]
        assert mirror.tool_result == "3 hits"
        # The mirror is a COPY -- the original chip is still exactly where
        # the trace tree put it, nested under the live Task chip.
        original_task_chip = pane._subagents["task-1"]
        original_sub_chip = next(
            c for c in original_task_chip.subcalls.children if c.call_id == "sub-1"
        )
        assert mirror is not original_sub_chip
        assert original_sub_chip in original_task_chip.subcalls.children

        engine.open_gate.set()
        engine.finish_gate.set()
        await _wait(pilot, lambda: not pane.turn_in_flight)


@pytest.mark.asyncio
async def test_reclicking_a_running_subagent_focuses_the_open_tab(tmp_path):
    engine = GatedSubagentEngine()
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane, tab = await _start_and_open(app, pilot)

        # Switch away (a real click on the session tab's own header --
        # ctrl+left/right cycle SESSION tabs only, and with just one open
        # here there is nothing for them to cycle to), then click the
        # label again -- it should refocus the SAME tab rather than
        # opening a second one.
        tabbed = app.query_one("#session-tabs", TabbedContent)
        await pilot.click(tabbed.get_tab(pane.tab_id))
        await pilot.pause()
        assert app.active_pane is pane

        line = pane.query_one("#subagent-line", SubagentLine)
        await pilot.click(line, offset=(2, 0))
        await pilot.pause()
        tabbed = app.query_one("#session-tabs", TabbedContent)
        assert tabbed.active == tab.id
        assert len(pane._transcript_tabs) == 1

        engine.open_gate.set()
        engine.finish_gate.set()
        await _wait(pilot, lambda: not pane.turn_in_flight)


@pytest.mark.asyncio
async def test_live_events_route_into_the_open_tab(tmp_path):
    engine = GatedSubagentEngine()
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane, tab = await _start_and_open(app, pilot)
        assert "sub-2" not in tab.mirror_chips

        engine.open_gate.set()  # releases the second nested call (sub-2)
        # Wait for the LAST event of that sequence (sub-2's own result) --
        # events land strictly in order within one turn, so its presence
        # guarantees the tool_call and the text_delta ahead of it in the
        # script already landed too, without a race on which of the three
        # a looser wait condition happens to observe first.
        assert await _wait(
            pilot,
            lambda: tab.mirror_chips.get("sub-2") is not None
            and tab.mirror_chips["sub-2"].tool_result == "200 lines",
        )
        assert "more narration" in tab._narration_text

        engine.finish_gate.set()
        await _wait(pilot, lambda: not pane.turn_in_flight)


@pytest.mark.asyncio
async def test_completion_marks_done_and_unseen_in_background(tmp_path):
    engine = GatedSubagentEngine()
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane, tab = await _start_and_open(app, pilot)
        engine.open_gate.set()
        assert await _wait(pilot, lambda: "sub-2" in tab.mirror_chips)

        # Step away from the transcript tab BEFORE it finishes (a real
        # click on the session tab's own header).
        tabbed = app.query_one("#session-tabs", TabbedContent)
        await pilot.click(tabbed.get_tab(pane.tab_id))
        await pilot.pause()
        assert app.active_pane is pane

        engine.finish_gate.set()
        assert await _wait(pilot, lambda: tab.done)
        assert _tab(app, tab.id).label.plain.endswith("✓")
        assert _tab(app, tab.id).has_class("-done-unseen")

        # Looking at it clears the dot -- same convention as a SessionPane.
        tabbed = app.query_one("#session-tabs", TabbedContent)
        tabbed.active = tab.id
        await pilot.pause()
        assert not _tab(app, tab.id).has_class("-done-unseen")


@pytest.mark.asyncio
async def test_ctrl_w_closes_the_transcript_tab_without_a_stop_dialog(tmp_path):
    from doxa.app import CloseWithTurnRunning

    engine = GatedSubagentEngine()
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane, tab = await _start_and_open(app, pilot)
        tabbed = app.query_one("#session-tabs", TabbedContent)
        assert tabbed.active == tab.id
        assert pane.turn_in_flight  # the underlying turn is still running

        await pilot.press("ctrl+w")
        await pilot.pause()

        # The transcript tab is gone; the SESSION tab is untouched -- no
        # engine stop, no CloseWithTurnRunning modal (that is Ctrl+Q-only,
        # and even then only for a SessionPane).
        assert tab.id not in [p.id for p in app.query(SubagentTranscriptTab)]
        assert "task-1" not in pane._transcript_tabs
        assert not app.query(CloseWithTurnRunning)
        assert pane in app.panes()
        assert engine.finalized is False
        assert pane.turn_in_flight

        engine.open_gate.set()
        engine.finish_gate.set()
        await _wait(pilot, lambda: not pane.turn_in_flight)


@pytest.mark.asyncio
async def test_ctrl_q_closes_the_transcript_tab_and_never_ends_its_session(tmp_path):
    """v0.58.0. Ctrl+Q is "end this session and close its tab"; on a tab
    that HAS no session it did nothing at all, and the user was stuck on a
    read-only transcript pressing a close key that never closed anything.

    Two assertions, and the second is the one that keeps the keys honest:
    the tab closes, and the session that SPAWNED the subagent is not
    touched -- not finalized, not stopped, its turn still in flight. A key
    aimed at the visible tab must never reach past it to the pane
    underneath."""
    engine = GatedSubagentEngine()
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane, tab = await _start_and_open(app, pilot)
        tabbed = app.query_one("#session-tabs", TabbedContent)
        assert tabbed.active == tab.id
        assert pane.turn_in_flight

        await pilot.press("ctrl+q")
        assert await _wait(
            pilot, lambda: tab.id not in [p.id for p in app.query(SubagentTranscriptTab)]
        )
        assert "task-1" not in pane._transcript_tabs

        # The owning session is untouched: still mounted, still running.
        assert pane in app.panes()
        assert engine.finalized is False
        assert pane.turn_in_flight

        engine.open_gate.set()
        engine.finish_gate.set()
        await _wait(pilot, lambda: not pane.turn_in_flight)


@pytest.mark.asyncio
async def test_transcript_tab_gets_no_provider_glyph(tmp_path):
    from doxa.app import PROVIDER_GLYPHS

    engine = GatedSubagentEngine()
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane, tab = await _start_and_open(app, pilot)

        label = _tab(app, tab.id).label.plain
        assert not label.startswith(PROVIDER_GLYPHS["claude"])
        assert label == "explore the auth module"

        engine.open_gate.set()
        engine.finish_gate.set()
        await _wait(pilot, lambda: not pane.turn_in_flight)


@pytest.mark.asyncio
async def test_closing_the_session_takes_its_open_transcript_tabs_with_it(tmp_path):
    engines: list[GatedSubagentEngine] = []

    def make() -> GatedSubagentEngine:
        engines.append(GatedSubagentEngine())
        return engines[-1]

    app = DoxaApp(cwd=str(tmp_path), engine_factory=make, new_session_factory=make)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        pane, tab = await _start_and_open(app, pilot)
        second_pane_id = pane.tab_id
        second_engine = engines[-1]

        # Switch focus back onto the SESSION tab itself (the transcript
        # tab it owns is currently active, straight out of _start_and_open)
        # so Ctrl+W's path (active_pane is a SessionPane) takes the normal
        # detach branch -- which must take the open transcript tab down
        # with it.
        tabbed = app.query_one("#session-tabs", TabbedContent)
        await pilot.click(tabbed.get_tab(second_pane_id))
        await pilot.pause()
        assert app.active_pane is pane
        await app.action_close_tab()
        await pilot.pause()

        assert tab.id not in [p.id for p in app.query(SubagentTranscriptTab)]
        assert second_pane_id not in [p.id for p in app.panes()]

        second_engine.open_gate.set()
        second_engine.finish_gate.set()

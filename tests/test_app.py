"""Headless Textual pilot test: mounts DoxaApp with a scripted FakeEngine
(no real SDK client underneath), submits one prompt, and asserts the turn
block + tool chip appear live and the chip's body lazily formats on first
expand. Follows PHASE0_FINDINGS.md SS4's proven pattern (run_test()'s Pilot
harness, no real terminal needed) -- same shape as spike/03_textual_marriage.py.
"""

from __future__ import annotations

import pytest

from doxa.app import DoxaApp, SystemBlock, ToolChip, TurnBlock
from doxa.engine import EngineEvent
from tests.fakes import FakeEngine

SCRIPT = [
    EngineEvent("turn_started", {}),
    EngineEvent("text_delta", {"text": "The answer is "}),
    EngineEvent("text_delta", {"text": "4."}),
    EngineEvent("tool_call", {"id": "t1", "name": "calculator_add", "input": {"a": 2, "b": 2}}),
    EngineEvent("tool_result", {
        "id": "t1", "name": "calculator_add", "result_summary": "4",
        "is_error": False, "duration_ms": 12,
    }),
    EngineEvent("turn_done", {
        "cost_usd": 0.002, "duration_ms": 250, "is_error": False,
        "session_cost_usd": 0.002, "ctx_percentage": 8.0,
    }),
]


@pytest.mark.asyncio
async def test_turn_block_and_tool_chip_appear_live(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: FakeEngine(SCRIPT),
    )

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.engine is not None and app.engine.started is True

        app.query_one("#prompt-input").value = "what is 2+2?"
        await pilot.press("enter")

        for _ in range(100):
            blocks = list(app.query(TurnBlock))
            if blocks and blocks[0].assistant_text == "The answer is 4.":
                break
            await pilot.pause(0.02)

        turn_blocks = list(app.query(TurnBlock))
        assert len(turn_blocks) == 1
        block = turn_blocks[0]
        assert block.assistant_text == "The answer is 4."
        assert block.prompt_text == "what is 2+2?"

        chips = list(app.query(ToolChip))
        assert len(chips) == 1
        chip = chips[0]
        assert chip.tool_name == "calculator_add"
        assert chip.tool_result == "4"
        assert chip.is_error is False
        assert chip.collapsed is True
        # Lazy formatting: the body must not be formatted before the chip
        # is ever expanded.
        assert chip._formatted is False

        chip.collapsed = False
        await pilot.pause()
        assert chip._formatted is True
        assert "ARGS:" in chip._body.renderable
        assert "RESULT:\n4" in chip._body.renderable

        # Status bar reflects the finished turn.
        status = app.query_one("#status-bar").renderable
        assert "0.0020" in status or "$0.0020" in status
        assert "3 beliefs" in status


@pytest.mark.asyncio
async def test_tool_disabled_shows_in_status_area(monkeypatch, tmp_path):
    """Two-strikes containment is visible: the tool_disabled event mounts a
    system block and the status bar carries the small `⊘ toolname` note."""
    fake = FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        fake.disabled = ["lore_belief_search"]
        fake.push_peer_event(EngineEvent("tool_disabled", {
            "name": "lore_belief_search",
            "reason": "lore_belief_search failed: RuntimeError: belief db unavailable",
        }))

        for _ in range(100):
            if list(app.query(SystemBlock)):
                break
            await pilot.pause(0.02)

        blocks = list(app.query(SystemBlock))
        assert len(blocks) == 1
        assert "⊘" in blocks[0].text
        assert "lore_belief_search" in blocks[0].text

        status = str(app.query_one("#status-bar").renderable)
        assert "⊘ lore_belief_search" in status


@pytest.mark.asyncio
async def test_quit_finalizes_the_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: FakeEngine([]),
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        engine = app.engine
        assert engine is not None and engine.finalized is False
        await app.action_quit()
        assert engine.finalized is True

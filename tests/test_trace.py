"""Trace-tree tests: subagent activity nests under its Task chip.

What the SDK actually surfaces for Task-tool calls (measured against
claude-agent-sdk 0.2.x): every message a Task-spawned subagent emits --
StreamEvent, AssistantMessage, UserMessage -- carries
``parent_tool_use_id`` = the Task call's own tool_use id. (SubagentStart/
SubagentStop hooks exist too, but carry agent_id/agent_type with no direct
linkage to the Task tool_use id -- the message-level parent id is the one
reliable nesting key.) The engine forwards it as the optional ``parent_id``
on tool_call/tool_result/text_delta events; the TUI nests child chips
inside the parent chip's ``subcalls`` and buffers subagent prose for lazy
render. Redaction discipline: subagent text is trace material and passes
scrub_secrets engine-side before it reaches an event.
"""

from __future__ import annotations

import pytest

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from doxa.app import DoxaApp, ToolChip, TurnBlock
from doxa.engine import EngineEvent, SessionEngine
from tests.fakes import FakeEngine, factory_with_script

FAKE_AWS_KEY = "AKIAABCDEFGHIJKLMNOP"


def _task_script() -> list:
    """One turn: the main thread calls Task, the subagent streams text and
    runs a tool of its own, both finish."""
    return [
        AssistantMessage(
            content=[ToolUseBlock(
                id="task-1", name="Task",
                input={"description": "explore", "subagent_type": "Explore"},
            )],
            model="claude-haiku-4-5",
        ),
        StreamEvent(
            uuid="s1", session_id="s", parent_tool_use_id="task-1",
            event={"type": "content_block_delta",
                   "delta": {"type": "text_delta",
                             "text": f"scanning with {FAKE_AWS_KEY} now"}},
        ),
        AssistantMessage(
            content=[ToolUseBlock(id="sub-1", name="Grep", input={"pattern": "x"})],
            model="claude-haiku-4-5",
            parent_tool_use_id="task-1",
        ),
        UserMessage(
            content=[ToolResultBlock(tool_use_id="sub-1", content="2 hits",
                                     is_error=False)],
            parent_tool_use_id="task-1",
        ),
        UserMessage(content=[ToolResultBlock(
            tool_use_id="task-1", content="explored: 2 hits", is_error=False,
        )]),
        ResultMessage(
            subtype="success", duration_ms=99, duration_api_ms=90, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.002,
        ),
    ]


@pytest.mark.asyncio
async def test_engine_tags_subagent_events_with_parent_id(tmp_path):
    factory, _created = factory_with_script(_task_script())
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    events = [ev async for ev in engine.send("explore the repo")]

    task_call = next(e for e in events if e.type == "tool_call" and e.data["id"] == "task-1")
    assert "parent_id" not in task_call.data  # main-thread call: top level

    sub_call = next(e for e in events if e.type == "tool_call" and e.data["id"] == "sub-1")
    assert sub_call.data["parent_id"] == "task-1"

    sub_result = next(e for e in events if e.type == "tool_result" and e.data["id"] == "sub-1")
    assert sub_result.data["parent_id"] == "task-1"
    task_result = next(e for e in events if e.type == "tool_result" and e.data["id"] == "task-1")
    assert "parent_id" not in task_result.data

    sub_text = next(e for e in events if e.type == "text_delta")
    assert sub_text.data["parent_id"] == "task-1"
    # Trace redaction: subagent prose is scrubbed BEFORE it reaches an event.
    assert FAKE_AWS_KEY not in sub_text.data["text"]
    assert "[REDACTED" in sub_text.data["text"]
    await engine.finalize()


TURN_EVENTS = [
    EngineEvent("turn_started", {}),
    EngineEvent("tool_call", {
        "id": "task-1", "name": "Task",
        "input": {"description": "explore", "subagent_type": "Explore"},
    }),
    EngineEvent("text_delta", {"text": "scanning [REDACTED:aws] now", "parent_id": "task-1"}),
    EngineEvent("tool_call", {
        "id": "sub-1", "name": "Grep", "input": {"pattern": "x"},
        "parent_id": "task-1",
    }),
    EngineEvent("tool_result", {
        "id": "sub-1", "name": "Grep", "result_summary": "2 hits",
        "is_error": False, "duration_ms": 7, "parent_id": "task-1",
    }),
    EngineEvent("text_delta", {"text": "done here."}),
    EngineEvent("tool_result", {
        "id": "task-1", "name": "Task", "result_summary": "explored: 2 hits",
        "is_error": False, "duration_ms": 90,
    }),
    EngineEvent("turn_done", {
        "cost_usd": 0.002, "duration_ms": 99, "is_error": False,
        "session_cost_usd": 0.002, "ctx_percentage": 3.0,
    }),
]


async def _run_task_turn(app, pilot):
    app.query_one("#prompt-input").value = "explore the repo"
    await pilot.press("enter")
    for _ in range(100):
        chips = {c.call_id: c for c in app.query(ToolChip)}
        if "task-1" in chips and chips["task-1"].tool_result is not None:
            return chips
        await pilot.pause(0.02)
    raise AssertionError("task chip never completed")


@pytest.mark.asyncio
async def test_subagent_chips_nest_under_the_task_chip(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: FakeEngine(list(TURN_EVENTS)),
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        chips = await _run_task_turn(app, pilot)
        task, sub = chips["task-1"], chips["sub-1"]

        # Tree shape: the subagent's chip lives INSIDE the Task chip's
        # subcalls container, not beside it in the turn's tool strip.
        assert sub in task.subcalls.children
        block = app.query_one(TurnBlock)
        # Top-level chips compact behind the turn's ONE "Tool calls (N)"
        # section (item (b)) -- the Task chip lives inside it, the
        # subagent chip does not (it is nested under the Task chip
        # instead, asserted above).
        assert block.tool_section is not None
        assert task in block.tool_section.chips.children
        assert sub not in block.tool_section.chips.children

        # Timing/args/results on both levels.
        assert task.duration_ms == 90 and sub.duration_ms == 7
        assert sub.tool_result == "2 hits"

        # The subagent's narration went to the trace, not the turn prose.
        assert block.assistant_text == "done here."
        assert task._sub_text == "scanning [REDACTED:aws] now"


@pytest.mark.asyncio
async def test_trace_tree_folds_and_formats_lazily(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: FakeEngine(list(TURN_EVENTS)),
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        chips = await _run_task_turn(app, pilot)
        task, sub = chips["task-1"], chips["sub-1"]

        # Both levels start folded and unformatted -- the lazy discipline
        # holds all the way down the tree.
        assert task.collapsed is True and sub.collapsed is True
        assert task._formatted is False and sub._formatted is False
        assert str(task._subout.renderable) == ""  # buffered, not rendered

        task.collapsed = False
        await pilot.pause()
        assert task._formatted is True
        assert "SUBAGENT:" in str(task._subout.renderable)
        assert "[REDACTED:aws]" in str(task._subout.renderable)
        assert sub._formatted is False  # the child only pays when opened

        sub.collapsed = False
        await pilot.pause()
        assert sub._formatted is True
        assert "RESULT:\n2 hits" in str(sub._body.renderable)


@pytest.mark.asyncio
async def test_unknown_parent_degrades_to_top_level(monkeypatch, tmp_path):
    """A parent id the pane has never seen (ring truncation on replay):
    the child chip mounts at top level rather than vanishing."""
    orphan_events = [
        EngineEvent("turn_started", {}),
        EngineEvent("tool_call", {
            "id": "sub-9", "name": "Grep", "input": {}, "parent_id": "gone-1",
        }),
        EngineEvent("tool_result", {
            "id": "sub-9", "name": "Grep", "result_summary": "ok",
            "is_error": False, "duration_ms": 1, "parent_id": "gone-1",
        }),
        EngineEvent("turn_done", {
            "cost_usd": 0.0, "duration_ms": 5, "is_error": False,
            "session_cost_usd": 0.0, "ctx_percentage": 1.0,
        }),
    ]
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: FakeEngine(orphan_events),
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "go"
        await pilot.press("enter")
        for _ in range(100):
            chips = list(app.query(ToolChip))
            if chips and chips[0].tool_result is not None:
                break
            await pilot.pause(0.02)
        block = app.query_one(TurnBlock)
        assert block.tool_section is not None
        assert [
            c.call_id for c in block.tool_section.chips.children
            if isinstance(c, ToolChip)
        ] == ["sub-9"]

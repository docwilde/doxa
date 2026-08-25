"""v0.25.0 reasoning stream: the model's summarized thinking, in a
collapsed "Reasoning" fold above the response body -- same idiom as the
v0.13.0 "Tool calls (N)" section (ToolCallsSection), applied to
doxa.app.ReasoningSection instead.

Pre-work finding this feature rests on (see doxa.engine.show_reasoning's
docstring for the full citation): the installed claude_agent_sdk exposes
``ClaudeAgentOptions.thinking`` (``ThinkingConfig``: adaptive/enabled/
disabled, with an optional ``display`` of "summarized"/"omitted"). Current
models default ``display`` to "omitted" -- an empty ``thinking`` string --
so the SDK returns NOTHING to stream unless a caller explicitly asks for
``display: "summarized"``. Before this feature, doxa.engine.send() already
received a StreamEvent for every ``thinking_delta`` (the raw Anthropic
stream event, ``StreamEvent.event`` is a passthrough dict) but its
content_block_delta branch only ever read ``delta.get("text")`` -- which a
thinking delta never sets (it sets ``delta["thinking"]`` instead) -- so
the content was silently dropped on the floor. test_engine_wires_thinking_
into_build_options and test_thinking_delta_becomes_reasoning_delta below
are the two halves of that fix.
"""

from __future__ import annotations

import pytest

from claude_agent_sdk import ResultMessage, StreamEvent

from doxa.app import DoxaApp, ReasoningSection, ThinkingMarker, ToolChip, TurnBlock
from doxa.engine import EngineEvent, SessionEngine
from tests.fakes import FakeEngine, factory_with_script

FAKE_AWS_KEY = "AKIAABCDEFGHIJKLMNOP"


# -- engine: options + StreamEvent parsing -----------------------------------


def test_build_options_requests_summarized_adaptive_thinking_by_default(tmp_path):
    """show_reasoning defaults ON (config.py's show_reasoning Setting,
    kind bool_on) -- _build_options must assert the documented way to get
    VISIBLE reasoning text: {"type": "adaptive", "display": "summarized"}.
    Plain {"type": "adaptive"} alone would leave display at its "omitted"
    default on current models and stream nothing at all."""
    eng = SessionEngine(cwd=str(tmp_path))
    options = eng._build_options()
    assert options.thinking == {"type": "adaptive", "display": "summarized"}


def test_show_reasoning_off_omits_thinking_key_entirely(monkeypatch, tmp_path):
    """Off must NOT assert thinking={"type": "disabled"} -- Claude Fable 5,
    Claude Mythos 5 and Claude Mythos Preview reject that outright (thinking
    cannot be turned off on those models), and self.model is usually still
    None at _build_options time (the real model is only known once the
    CLI's init SystemMessage arrives, after connect), so there is no way to
    special-case around it here. Off means "assert nothing" -- the one
    value guaranteed not to break on any model."""
    monkeypatch.setenv("DOXA_SHOW_REASONING", "0")
    eng = SessionEngine(cwd=str(tmp_path))
    options = eng._build_options()
    assert options.thinking is None


@pytest.mark.asyncio
async def test_thinking_delta_becomes_reasoning_delta(tmp_path):
    """The raw StreamEvent shape confirmed against Anthropic's own
    streaming docs: {"type": "content_block_delta", "delta": {"type":
    "thinking_delta", "thinking": "..."}}. A plain text_delta in the same
    turn must still become text_delta, unaffected."""
    script = [
        StreamEvent(
            uuid="s1", session_id="s",
            event={"type": "content_block_delta",
                   "delta": {"type": "thinking_delta",
                             "thinking": "Let me work through this step by step."}},
        ),
        StreamEvent(
            uuid="s2", session_id="s",
            event={"type": "content_block_delta",
                   "delta": {"type": "text_delta", "text": "The answer is 4."}},
        ),
        ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=9, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ]
    factory, _created = factory_with_script(script)
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    events = [ev async for ev in engine.send("what is 2+2?")]

    reasoning = [e for e in events if e.type == "reasoning_delta"]
    assert len(reasoning) == 1
    assert reasoning[0].data == {"text": "Let me work through this step by step."}

    text = [e for e in events if e.type == "text_delta"]
    assert len(text) == 1
    assert text[0].data == {"text": "The answer is 4."}
    await engine.finalize()


@pytest.mark.asyncio
async def test_subagent_reasoning_is_scrubbed_and_tagged_with_parent_id(tmp_path):
    """Parity with text_delta's own subagent-trace convention
    (test_trace.py): a thinking delta carrying parent_tool_use_id is
    secret-scrubbed and tagged parent_id, exactly like subagent text."""
    script = [
        StreamEvent(
            uuid="s1", session_id="s", parent_tool_use_id="task-1",
            event={"type": "content_block_delta",
                   "delta": {"type": "thinking_delta",
                             "thinking": f"checking {FAKE_AWS_KEY} now"}},
        ),
        ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=9, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ]
    factory, _created = factory_with_script(script)
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    events = [ev async for ev in engine.send("explore")]

    reasoning = next(e for e in events if e.type == "reasoning_delta")
    assert reasoning.data["parent_id"] == "task-1"
    assert FAKE_AWS_KEY not in reasoning.data["text"]
    assert "[REDACTED" in reasoning.data["text"]
    await engine.finalize()


@pytest.mark.asyncio
async def test_empty_thinking_delta_yields_no_event(tmp_path):
    """display: "omitted" streams a thinking block with an empty string
    (per Anthropic's docs) rather than skipping content_block_delta
    altogether -- an empty chunk must not become a hide-at-zero-breaking
    empty reasoning_delta."""
    script = [
        StreamEvent(
            uuid="s1", session_id="s",
            event={"type": "content_block_delta",
                   "delta": {"type": "thinking_delta", "thinking": ""}},
        ),
        ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=9, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ]
    factory, _created = factory_with_script(script)
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    events = [ev async for ev in engine.send("hi")]
    assert not [e for e in events if e.type == "reasoning_delta"]
    await engine.finalize()


# -- app: TurnBlock / ReasoningSection ----------------------------------------


async def _mount_bare_turn(app: DoxaApp, prompt: str = "hi") -> TurnBlock:
    from textual.containers import VerticalScroll

    assert app.active_pane is not None
    block_list = app.active_pane.query_one("#block-list", VerticalScroll)
    block = TurnBlock(prompt)
    await block_list.mount(block)
    return block


@pytest.mark.asyncio
async def test_zero_reasoning_grows_no_section(monkeypatch, tmp_path):
    """Hide-at-zero: a turn with no reasoning_delta events (show_reasoning
    off, or the model just didn't think) never gets a ReasoningSection at
    all -- same convention as ToolCallsSection."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app)
        assert block.reasoning_section is None
        assert not list(app.query(ReasoningSection))


@pytest.mark.asyncio
async def test_reasoning_section_collapsed_by_default_and_mounted_above_body(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app)
        await block.append_reasoning("Let me think about this.")

        assert block.reasoning_section is not None
        assert block.reasoning_section.collapsed is True
        # Reasoning precedes the answer -- above .turn-body in the tree
        # (Collapsible wraps its children in an internal Contents node, so
        # walk the full descendant order rather than block.children).
        descendants = list(block.walk_children())
        assert descendants.index(block.reasoning_holder) < descendants.index(block.body)


@pytest.mark.asyncio
async def test_reasoning_header_updates_live_with_char_count_while_collapsed(
    monkeypatch, tmp_path,
):
    """Match ToolCallsSection's "Tool calls (N)" live-count title-rewrite
    pattern: cheap, and it must update WHILE COLLAPSED (the whole point of
    the header is to say something is happening without expanding)."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app)

        await block.append_reasoning("12345")
        assert block.reasoning_section.collapsed is True
        assert "Reasoning (5 chars)" in str(block.reasoning_section.title)

        await block.append_reasoning("67890")
        assert "Reasoning (10 chars)" in str(block.reasoning_section.title)
        assert block.reasoning_section.collapsed is True  # never auto-expands either


@pytest.mark.asyncio
async def test_expanded_reasoning_stays_expanded_as_more_arrives(monkeypatch, tmp_path):
    """If the user expands the section mid-turn it MUST stay expanded as
    further reasoning streams in -- never auto-collapse under the cursor
    (same invariant test_restyle.py asserts for ToolCallsSection)."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app)
        await block.append_reasoning("first chunk. ")
        block.reasoning_section.collapsed = False
        await pilot.pause()
        assert block.reasoning_section.collapsed is False

        await block.append_reasoning("second chunk.")
        await pilot.pause()
        assert block.reasoning_section.collapsed is False
        assert "Reasoning (26 chars)" in str(block.reasoning_section.title)


@pytest.mark.asyncio
async def test_reasoning_arrival_moves_the_marker_into_the_reasoning_phase(
    monkeypatch, tmp_path,
):
    """v0.25.0 had the first reasoning_delta HIDE the marker: it was
    subsumed, on the grounds that a live "Reasoning (N chars)" header IS
    the "something is happening" signal at that point.

    v0.51.0 reverses that call, and the reversal is the feature rather
    than a side effect of one. A header whose count has stopped moving
    reads exactly like a finished one, and the phase AFTER reasoning -- a
    streaming answer -- offers the reader no progress signal at all,
    because the text is the thing they are trying to read. So the marker
    lives for the whole turn now and names the phase it is in; see
    ThinkingMarker's docstring for the full argument and
    tests/test_transcript_density.py for the appearance assertions. What
    v0.25.0 was really protecting is UNCHANGED: the marker still arms no
    timer, and it is still ONE widget rather than a second thing saying
    "working" beside the reasoning header."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app)
        assert block.thinking.display is not False  # visible before anything arrives

        await block.append_reasoning("thinking...")
        assert block.thinking.display is True
        assert block.thinking.phase == "reasoning"
        assert block.thinking.auto_refresh is None

        # ...and it hands over cleanly when the answer itself starts.
        await block.append_text("here goes")
        assert block.thinking.phase == "generating"

        # A finished turn still takes it away. That never moved.
        await block.mark_done(0.001, 10, False)
        assert block.thinking.display is False


@pytest.mark.asyncio
async def test_mark_done_stops_the_reasoning_stream(monkeypatch, tmp_path):
    """A finished turn must not leave the reasoning section's background
    write task running any more than the response body's may (v0.13.0
    already established this for .body; this feature carries the same
    rule for .reasoning_section) -- no new idle-CPU source."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app)
        await block.append_reasoning("some reasoning")
        await block.mark_done(0.001, 10, False)

        assert block.reasoning_section._stream._stopped is True
        assert block.reasoning_section._stream._task is None


@pytest.mark.asyncio
async def test_mark_done_on_turn_with_no_reasoning_does_not_blow_up(monkeypatch, tmp_path):
    """Lazy like everything else: a turn that never streams reasoning
    never creates a ReasoningSection, and mark_done on it must not crash
    on the None case (mirrors test_restyle.py's equivalent for .body)."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app)
        assert block.reasoning_section is None
        await block.mark_done(0.0, 5, False)
        assert block.reasoning_section is None


# -- FakeEngine parity: a full scripted turn through DoxaApp ------------------

REASONING_TURN_SCRIPT = [
    EngineEvent("turn_started", {}),
    EngineEvent("reasoning_delta", {"text": "First, let me consider the "}),
    EngineEvent("reasoning_delta", {"text": "two numbers involved."}),
    EngineEvent("text_delta", {"text": "The answer is 4."}),
    EngineEvent("tool_call", {"id": "t1", "name": "calculator_add", "input": {"a": 2, "b": 2}}),
    EngineEvent("tool_result", {
        "id": "t1", "name": "calculator_add", "result_summary": "4",
        "is_error": False, "duration_ms": 5,
    }),
    EngineEvent("turn_done", {
        "cost_usd": 0.001, "duration_ms": 90, "is_error": False,
        "session_cost_usd": 0.001, "ctx_percentage": 2.0,
    }),
]


@pytest.mark.asyncio
async def test_reasoning_delta_flows_end_to_end_through_fake_engine(monkeypatch, tmp_path):
    """FakeEngine parity by contract (see tests/fakes.py's module docstring):
    it replays EngineEvent scripts verbatim, so a script that includes
    reasoning_delta already exercises the real dispatch path
    (SessionPane._handle_event) exactly like text_delta/tool_call do,
    with no FakeEngine code change required."""
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: FakeEngine(list(REASONING_TURN_SCRIPT)),
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "what is 2+2?"
        await pilot.press("enter")

        for _ in range(100):
            blocks = list(app.query(TurnBlock))
            if blocks and blocks[0].assistant_text == "The answer is 4.":
                break
            await pilot.pause(0.02)

        block = list(app.query(TurnBlock))[0]
        assert block.reasoning_section is not None
        assert "Reasoning (48 chars)" in str(block.reasoning_section.title)
        assert list(app.query(ToolChip))  # unaffected: tool calls still land

        # Turn finished -- both streams (body + reasoning) are stopped.
        assert block._stream._stopped is True
        assert block.reasoning_section._stream._stopped is True

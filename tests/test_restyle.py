"""v0.13.0 visual restyle: tool-call compaction (item b) and markdown
rendering for the agent's streamed response (item c). Item (a) -- boxes to
background tints -- is CSS-only and covered by eyeballing the regenerated
screenshots (scripts/screenshot.py), not by pytest. Item (d) -- the
screenshot regen itself -- likewise has no unit coverage.
"""

from __future__ import annotations

import pytest

from textual.containers import VerticalScroll
from textual.widgets._markdown import MarkdownFence, MarkdownTable

from doxa.app import DoxaApp, ToolCallsSection, ToolChip, TurnBlock
from tests.fakes import FakeEngine


async def _mount_bare_turn(app: DoxaApp, prompt: str = "hi") -> TurnBlock:
    """Mount a TurnBlock directly, bypassing the engine event stream, so
    tests can drive add_tool_chip/append_text/mark_done with exact control
    over ordering -- the same shape _handle_event drives them in, just
    without a FakeEngine script's own timing."""
    assert app.active_pane is not None
    block_list = app.active_pane.query_one("#block-list", VerticalScroll)
    block = TurnBlock(prompt)
    await block_list.mount(block)
    return block


# -- (b) tool-call compaction -----------------------------------------------


@pytest.mark.asyncio
async def test_zero_tool_calls_grows_no_section(monkeypatch, tmp_path):
    """Hide-at-zero: a turn with no tool calls never gets a ToolCallsSection
    at all -- same convention as the git/usage/peers/disabled-tools chips."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app)
        assert block.tool_section is None
        assert not list(app.query(ToolCallsSection))


@pytest.mark.asyncio
async def test_tool_calls_compact_behind_one_section_with_live_count(monkeypatch, tmp_path):
    """N top-level chips land inside ONE "Tool calls (N)" section, collapsed
    by default, whose title updates live (a cheap rewrite) as each chip
    mounts -- not a repaint storm, not a fresh section per chip."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app)

        chip1 = ToolChip("t1", "grep", {"pattern": "x"})
        await block.add_tool_chip(chip1)
        assert block.tool_section is not None
        assert "Tool calls (1)" in str(block.tool_section.title)
        assert block.tool_section.collapsed is True

        chip2 = ToolChip("t2", "read", {"path": "a"})
        await block.add_tool_chip(chip2)
        assert "Tool calls (2)" in str(block.tool_section.title)

        # ONE section for the whole turn, both chips inside it.
        assert len(list(app.query(ToolCallsSection))) == 1
        assert chip1 in block.tool_section.chips.children
        assert chip2 in block.tool_section.chips.children


@pytest.mark.asyncio
async def test_expanded_section_stays_expanded_as_more_chips_mount(monkeypatch, tmp_path):
    """If the user expands the section mid-turn it MUST stay expanded as
    further chips arrive -- never auto-collapse under the cursor."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app)
        await block.add_tool_chip(ToolChip("t1", "grep", {}))
        block.tool_section.collapsed = False
        await pilot.pause()
        assert block.tool_section.collapsed is False

        await block.add_tool_chip(ToolChip("t2", "read", {}))
        await pilot.pause()
        assert block.tool_section.collapsed is False
        assert "Tool calls (2)" in str(block.tool_section.title)


@pytest.mark.asyncio
async def test_subagent_trace_tree_unaffected_by_compaction(monkeypatch, tmp_path):
    """A Task chip's own subcalls still nest inside ITS subcalls container,
    not inside the turn's ToolCallsSection -- compaction wraps top-level
    chips only, the trace tree underneath is untouched."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app)
        task = ToolChip("task-1", "Task", {"description": "explore"})
        await block.add_tool_chip(task)
        sub = ToolChip("sub-1", "Grep", {"pattern": "x"})
        await task.subcalls.mount(sub)

        assert sub in task.subcalls.children
        assert sub not in block.tool_section.chips.children
        assert task in block.tool_section.chips.children


# -- (c) markdown rendering for agent prose ----------------------------------


@pytest.mark.asyncio
async def test_markdown_streaming_survives_chunk_boundaries(monkeypatch, tmp_path):
    """Chunks split mid-table-row and mid-code-fence -- the real shape of
    an LLM's text_delta stream -- must still render as one table and one
    fence, not garbled fragments, and bold text renders as a real style
    span rather than literal asterisks."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app)

        chunks = [
            "Here is a **bold** claim with `inline` code.\n\n",
            "| a | b |\n", "|---|---|\n", "| 1 | 2 |\n\n",
            "```python\n", "print(1)\n", "```\n",
        ]
        for chunk in chunks:
            await block.append_text(chunk)
        await pilot.pause()

        # The plain-text accumulator (used by the title/tab-naming code
        # elsewhere) still holds the raw, unparsed concatenation.
        assert block.assistant_text == "".join(chunks)

        assert len(list(block.body.query(MarkdownFence))) == 1
        assert len(list(block.body.query(MarkdownTable))) == 1

        paragraph = block.body.children[0]
        content = paragraph.renderable
        assert "**" not in str(content)
        assert any(span.style == ".strong" for span in content.spans)

        await block.mark_done(0.001, 10, False)
        # mark_done stops the stream's one background task -- a finished
        # turn must not leave it running any more than a finished turn
        # may leave a Textual auto-refresh timer running.
        assert block._stream._stopped is True
        assert block._stream._task is None


@pytest.mark.asyncio
async def test_user_prompt_stays_literal_plain_text(monkeypatch, tmp_path):
    """The prompt slice must never reflow through markdown -- typed
    "**bold**" stays literal asterisks in the fold header, unlike the
    agent's response, which DOES parse the same syntax (previous test)."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app, prompt="please **bold** this")
        assert "**bold**" in block.title


@pytest.mark.asyncio
async def test_no_stream_created_for_a_turn_with_no_text(monkeypatch, tmp_path):
    """Lazy like everything else here: a turn that never streams text (all
    tool calls, no prose) never creates a MarkdownStream -- and mark_done
    on it must not blow up on the None case."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app)
        assert block._stream is None
        await block.mark_done(0.0, 5, False)
        assert block._stream is None

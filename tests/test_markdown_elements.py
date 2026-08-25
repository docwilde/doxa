"""Markdown ELEMENT coverage for agent prose.

`tests/test_restyle.py::test_markdown_streaming_survives_chunk_boundaries`
already guards the streaming MECHANISM -- that a table and a fence split
across `text_delta` boundaries still arrive as one `MarkdownTable` and one
`MarkdownFence`, and that `**bold**` becomes a style span rather than
literal asterisks. That is the hard part, and it is covered.

What was NOT covered is everything the model actually writes the rest of
the time: headings, bullet and ordered lists, NESTED lists, block quotes,
horizontal rules, inline code. Those are separate widget classes in
Textual's markdown renderer, and "the stream works" says nothing about
whether they render -- exactly the gap that let the v0.28.0 zero-height
buttons ship green (structure asserted, appearance never looked at).

So each test here asserts the WIDGET the element is supposed to produce,
and where the distinction is meaningful, that the source markup is gone
from the rendered text -- a heading that still reads "## Heading" is not a
heading. Every one drives `TurnBlock.append_text`, the same path a real
`text_delta` takes, rather than constructing a `Markdown` widget directly:
the point is what a user sees at the end of a turn, not that Textual's
parser works.
"""

from __future__ import annotations

import pytest

from textual.containers import VerticalScroll
from textual.widgets._markdown import (
    MarkdownBlockQuote,
    MarkdownBulletList,
    MarkdownFence,
    MarkdownH1,
    MarkdownH2,
    MarkdownHorizontalRule,
    MarkdownOrderedList,
    MarkdownParagraph,
)

from doxa.app import DoxaApp, TurnBlock
from tests.fakes import FakeEngine


async def _turn(app: DoxaApp, text: str) -> TurnBlock:
    """Mount a turn and stream `text` through the real append path."""
    assert app.active_pane is not None
    block_list = app.active_pane.query_one("#block-list", VerticalScroll)
    block = TurnBlock("hi")
    await block_list.mount(block)
    await block.append_text(text)
    return block


def _rendered_text(block: TurnBlock) -> str:
    """Everything the markdown body actually draws, as one string."""
    parts: list[str] = []
    for child in block.body.query(MarkdownParagraph):
        parts.append(str(child.renderable))
    for child in block.body.children:
        renderable = getattr(child, "renderable", None)
        if renderable is not None:
            parts.append(str(renderable))
    return "\n".join(parts)


@pytest.fixture
def app(monkeypatch, tmp_path) -> DoxaApp:
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    return DoxaApp(cwd=str(tmp_path))


@pytest.mark.asyncio
async def test_headings_render_as_heading_widgets_not_hashes(app):
    """`# H1` / `## H2` become heading widgets, and the hashes are gone --
    a heading still reading "## Plan" would mean the parser never ran."""
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _turn(app, "# Title\n\n## Plan\n\nprose after.\n")
        await pilot.pause()

        assert len(list(block.body.query(MarkdownH1))) == 1
        assert len(list(block.body.query(MarkdownH2))) == 1
        text = _rendered_text(block)
        assert "# Title" not in text
        assert "## Plan" not in text


@pytest.mark.asyncio
async def test_bullet_list_renders_as_a_list_widget(app):
    """A dash list is a `MarkdownBulletList`, not a paragraph of dashes."""
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _turn(app, "- first\n- second\n- third\n")
        await pilot.pause()

        lists = list(block.body.query(MarkdownBulletList))
        assert len(lists) == 1
        assert "- first" not in _rendered_text(block)


@pytest.mark.asyncio
async def test_ordered_list_renders_as_an_ordered_list_widget(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _turn(app, "1. one\n2. two\n3. three\n")
        await pilot.pause()

        assert len(list(block.body.query(MarkdownOrderedList))) == 1


@pytest.mark.asyncio
async def test_nested_list_nests_rather_than_flattening(app):
    """The one the model writes constantly and the one most likely to
    flatten: a sub-list must produce a list INSIDE a list, not two
    siblings. Asserting only "some list exists" would pass on a flattened
    render, so this asserts containment."""
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _turn(
            app,
            "- outer one\n"
            "    - inner a\n"
            "    - inner b\n"
            "- outer two\n",
        )
        await pilot.pause()

        lists = list(block.body.query(MarkdownBulletList))
        assert len(lists) >= 2, "a nested list should produce a nested list widget"

        outer = lists[0]
        nested = [lst for lst in lists[1:] if lst in outer.query(MarkdownBulletList)]
        assert nested, "the inner list is not a descendant of the outer one"


@pytest.mark.asyncio
async def test_block_quote_renders_as_a_quote_widget(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _turn(app, "> quoted claim\n\nplain after.\n")
        await pilot.pause()

        assert len(list(block.body.query(MarkdownBlockQuote))) == 1
        assert "> quoted" not in _rendered_text(block)


@pytest.mark.asyncio
async def test_horizontal_rule_renders_as_a_rule_widget(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _turn(app, "before\n\n---\n\nafter\n")
        await pilot.pause()

        assert len(list(block.body.query(MarkdownHorizontalRule))) == 1


@pytest.mark.asyncio
async def test_inline_code_is_styled_not_backticked(app):
    """Inline code keeps its text and loses its backticks -- the same
    claim the bold assertion makes in the streaming test, for the other
    span type the model uses on every other line."""
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _turn(app, "run `doxa attach` to reattach.\n")
        await pilot.pause()

        paragraph = block.body.query_one(MarkdownParagraph)
        content = paragraph.renderable
        assert "doxa attach" in str(content)
        assert "`" not in str(content)
        assert content.spans, "inline code produced no style span at all"


@pytest.mark.asyncio
async def test_a_realistic_mixed_answer_renders_every_element_together(app):
    """The elements above, in one message, in the shape a real answer
    arrives -- because a parser can handle each in isolation and still
    lose one when they are adjacent (the streaming test found exactly
    that class of bug at chunk boundaries)."""
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _turn(
            app,
            "## What I changed\n\n"
            "- fixed the **race**\n"
            "    - measured 40/40\n"
            "- left `_persist_tabset` alone\n\n"
            "> it was a different bug\n\n"
            "```python\n"
            "assert True\n"
            "```\n\n"
            "1. merge\n"
            "2. tag\n",
        )
        await pilot.pause()

        assert len(list(block.body.query(MarkdownH2))) == 1
        assert len(list(block.body.query(MarkdownBulletList))) >= 2
        assert len(list(block.body.query(MarkdownOrderedList))) == 1
        assert len(list(block.body.query(MarkdownBlockQuote))) == 1
        assert len(list(block.body.query(MarkdownFence))) == 1

        text = _rendered_text(block)
        for markup in ("## ", "```", "**race**"):
            assert markup not in text, f"{markup!r} survived into the rendered text"

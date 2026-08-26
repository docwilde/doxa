# SPDX-License-Identifier: AGPL-3.0-only
"""Reported: a long user prompt's fold title gets sliced mid-word at the
column edge, with no ellipsis, and the full prompt is reachable nowhere
else -- expanding the fold shows only the model's answer, never the
question that started it. ``TurnBlock`` (doxa/ui/transcript.py) fixed both
halves: the title is now cut at a WORD boundary with a trailing ellipsis,
against the box's OWN measured width (not a fixed guess), and whenever it
has to cut, the un-cut prompt grows a second home in the fold body
(``self.prompt_full``).

v0.28.0 bar throughout: assert rendered text and non-zero geometry, not
that an attribute merely holds a string.
"""

from __future__ import annotations

import pytest

from textual.containers import VerticalScroll

from doxa.app import DoxaApp, TurnBlock
from tests.fakes import FakeEngine


async def _mount_bare_turn(app: DoxaApp, prompt: str) -> TurnBlock:
    """Same helper test_restyle.py uses: a TurnBlock mounted directly, no
    engine event stream, full control over the prompt text."""
    assert app.active_pane is not None
    block_list = app.active_pane.query_one("#block-list", VerticalScroll)
    block = TurnBlock(prompt)
    await block_list.mount(block)
    return block


LONG_PROMPT = (
    "please take a careful look at the ingest spool handling and tell me "
    "whether an absent directory should really be treated as a fatal "
    "error or as an empty spool with zero rows to fold, because the "
    "nightly treatment tier keeps failing on a fresh checkout"
)


@pytest.mark.asyncio
async def test_short_prompt_shows_in_full_and_grows_no_second_home(monkeypatch, tmp_path):
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app, "hi there")
        await pilot.pause()
        assert "hi there" in block.title
        assert "…" not in block.title
        # Nothing lost, nothing to show twice.
        assert block.prompt_full.display is False


@pytest.mark.asyncio
async def test_narrow_terminal_truncates_the_title_at_a_word_boundary(
    monkeypatch, tmp_path,
):
    """The regression itself: at a column count the long prompt cannot
    possibly fit, the title must end in an ellipsis and never chop a word
    in half."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(50, 24)) as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app, LONG_PROMPT)
        await pilot.pause()
        title = str(block.title)
        assert title.endswith("…"), title
        # Whatever precedes the ellipsis is a PREFIX of the collapsed
        # prompt ending exactly on a word boundary -- never a slice that
        # lands inside a word the prompt actually contains.
        shown = title[len("▎ "):-1].rstrip()
        assert LONG_PROMPT.startswith(shown)
        # The character immediately after the shown text in the original
        # prompt is a space (or the shown text ends the prompt) -- i.e.
        # the cut never happened mid-word.
        assert shown == "" or LONG_PROMPT[len(shown):len(shown) + 1] in (" ", "")

        # The rendered CollapsibleTitle widget itself -- not just the
        # python attribute -- actually painted something, and it fits the
        # box Textual gave it (v0.28.0 bar: rendered geometry, not a
        # query match).
        title_widget = block.query_one("CollapsibleTitle")
        assert title_widget.region.height > 0
        rendered = str(title_widget.renderable)
        assert "…" in rendered


@pytest.mark.asyncio
async def test_truncated_prompt_is_reachable_in_full_in_the_fold_body(
    monkeypatch, tmp_path,
):
    """The part that matters more than the ellipsis (item 2): a title that
    had to cut must not be the only place the prompt exists. Expanding the
    fold (it starts expanded) must show the words the title dropped."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(50, 24)) as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app, LONG_PROMPT)
        await pilot.pause()
        assert "…" in block.title  # sanity: this run DID truncate

        assert block.prompt_full.display is True
        assert block.prompt_full.region.height > 0
        rendered = str(block.prompt_full.renderable)
        assert LONG_PROMPT in rendered


@pytest.mark.asyncio
async def test_title_retruncates_on_resize_not_a_stale_cut(monkeypatch, tmp_path):
    """Widen the terminal mid-session: the title must re-fit to the NEW
    width, not keep whatever it cut to at construction time."""
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(50, 24)) as pilot:
        await pilot.pause()
        block = await _mount_bare_turn(app, LONG_PROMPT)
        await pilot.pause()
        assert "…" in block.title
        assert block.prompt_full.display is True

        await pilot.resize_terminal(300, 24)
        await pilot.pause()
        assert "…" not in block.title
        assert LONG_PROMPT in block.title
        assert block.prompt_full.display is False

        await pilot.resize_terminal(50, 24)
        await pilot.pause()
        assert "…" in block.title
        assert block.prompt_full.display is True
        assert LONG_PROMPT in str(block.prompt_full.renderable)

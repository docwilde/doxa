# SPDX-License-Identifier: AGPL-3.0-only
"""Item K -- ``/context``: what is actually occupying the model's window.

The feature's whole premise is that the ctx% chip is one opaque number and
a user deserves the breakdown behind it. Its whole CONSTRAINT is that a
diagnostic surface may not lie: every figure ``/context`` prints is the
claude CLI's own accounting of its own request
(``ClaudeSDKClient.get_context_usage``), DOXA runs no tokenizer of its own,
and a component whose size can only be guessed at is either labelled for
what it really is or left out. These tests pin both halves --- what the
user sees, and the absence of anything invented.

The one measurement DOXA contributes is the LORE snapshot's size, and it is
reported in CHARACTERS, because that is what DOXA actually knows: the CLI
counts those tokens inside its own "system prompt" category and cannot tell
the appendix from the preset. ``test_a_component_the_cli_cannot_separate_
is_reported_in_characters`` is that rule.
"""

from __future__ import annotations

import json

import pytest

from claude_agent_sdk import ResultMessage

from doxa import commands, engine as engine_mod
from doxa.app import ContextBlock, DoxaApp, SlashComplete, SystemBlock
from doxa.engine import SessionEngine, context_breakdown
from doxa.ui.labels import (
    CONTEXT_GRID_TRACK,
    CONTEXT_UNAVAILABLE,
    GRID_CELL_WIDTH,
    GRID_CELLS,
    GRID_COLUMNS,
    GRID_ROWS,
    context_breakdown_text,
    context_grid_cells,
    context_grid_mode,
    context_grid_text,
    context_sources_text,
    ctx_text,
    help_text,
)
from tests.fakes import FakeEngine, factory_with_script

# A realistic get_context_usage reply, shaped exactly like the SDK's
# ContextUsageResponse. Everything DOXA renders comes from here; nothing
# DOXA renders is computed from anything else.
CTX_USAGE = {
    "categories": [
        {"name": "System prompt", "tokens": 3_200, "color": "blue"},
        {"name": "System tools", "tokens": 12_900, "color": "green"},
        {"name": "MCP tools", "tokens": 1_450, "color": "purple"},
        {"name": "Memory files", "tokens": 2_100, "color": "orange"},
        {"name": "Messages", "tokens": 41_000, "color": "red"},
        {"name": "Free space", "tokens": 119_350, "color": "grey"},
    ],
    "totalTokens": 60_650,
    "maxTokens": 180_000,
    "rawMaxTokens": 200_000,
    "percentage": 33.7,
    "model": "claude-opus-4-6",
    "isAutoCompactEnabled": True,
    "autoCompactThreshold": 160_000,
    "memoryFiles": [
        {"path": "/work/repo/CLAUDE.md", "type": "Project", "tokens": 2_100},
    ],
    "mcpTools": [
        {"name": "lore_belief_search", "serverName": "doxa_lore",
         "tokens": 380, "isLoaded": True},
    ],
    "agents": [{"agentType": "explore", "source": "builtin", "tokens": 900}],
    # The big one -- a pre-rendered grid of the categories above, which is
    # exactly what the normalizer exists to drop.
    "gridRows": [[{"color": "blue", "char": "█"}] * 60 for _ in range(20)],
}


def _breakdown(**kwargs) -> dict:
    return context_breakdown(CTX_USAGE, **kwargs)


async def _app(monkeypatch, tmp_path, fake=None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    fake = fake if fake is not None else FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    return DoxaApp(cwd=str(tmp_path)), fake


async def _run(app, pilot, line: str) -> str:
    app.query_one("#prompt-input").value = line
    before = len([b for b in app.query(SystemBlock) if b.id != "identity-block"])
    await pilot.press("enter")
    for _ in range(300):
        texts = [b.text for b in app.query(SystemBlock) if b.id != "identity-block"]
        if len(texts) > before:
            return texts[-1]
        await pilot.pause(0.02)
    raise AssertionError(f"{line!r} produced no output block")


# -- what the user sees ---------------------------------------------------


@pytest.mark.asyncio
async def test_context_shows_the_breakdown_in_the_transcript(monkeypatch, tmp_path):
    """The headline: every category the CLI reported is on screen with its
    own token count, and so is the window it sits in."""
    fake = FakeEngine([])
    fake.context_usage_result = _breakdown()
    app, fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/context")
    assert fake.context_usage_calls == 1
    for name in ("System prompt", "System tools", "MCP tools", "Messages",
                 "Free space"):
        assert name in text
    assert "41,000" in text        # the Messages category, verbatim
    assert "60,650 / 180,000" in text
    assert "33.7%" in text
    assert "claude-opus-4-6" in text


@pytest.mark.asyncio
async def test_context_names_the_memory_files_and_mcp_tools_that_cost_tokens(
    monkeypatch, tmp_path
):
    """The two decompositions an operator can act on: which CLAUDE.md got
    loaded, and what DOXA's own in-process MCP server costs."""
    fake = FakeEngine([])
    fake.context_usage_result = _breakdown()
    app, _fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/context")
    assert "/work/repo/CLAUDE.md" in text and "2,100" in text
    assert "doxa_lore: lore_belief_search" in text and "380" in text


@pytest.mark.asyncio
async def test_context_reaches_help_the_palette_and_the_autocomplete(
    monkeypatch, tmp_path
):
    """Registered in doxa/commands.py, so every surface gets it free --
    which is the point of there being one registry."""
    assert commands.find("/context") is not None
    assert "/context" in commands.interactive_names()
    assert "/context" in help_text()

    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "Context breakdown" in {e.label for e in app.doxa_commands()}
        for char in ("slash", "c", "o", "n"):
            await pilot.press(char)
        await pilot.pause()
        dropdown = app.query_one("#slash-complete", SlashComplete)
        assert dropdown.is_open is True
        assert "/context" in [c.name for c in dropdown.matches]


@pytest.mark.asyncio
async def test_context_is_not_the_same_question_as_usage(monkeypatch, tmp_path):
    """/usage is what the session has SPENT; /context is what it is
    CARRYING. Both exist because neither answers the other."""
    fake = FakeEngine([])
    fake.context_usage_result = _breakdown()
    app, _fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        usage = await _run(app, pilot, "/usage")
        context = await _run(app, pilot, "/context")
    assert "turns" in usage and "cost" in usage.lower()
    assert "turns" not in context
    assert "Free space" in context and "Free space" not in usage


# -- nothing invented -----------------------------------------------------


@pytest.mark.asyncio
async def test_context_reports_the_absence_instead_of_estimating(
    monkeypatch, tmp_path
):
    """A session that cannot be measured prints a sentence and no numbers.
    An invented breakdown in a diagnostic surface is worse than a missing
    one, and this is where that rule is enforced."""
    fake = FakeEngine([])
    fake.context_usage_result = None
    app, _fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/context")
    assert text == CONTEXT_UNAVAILABLE
    assert "estimated" in text or "Nothing is estimated" in text
    assert not any(char.isdigit() for char in text)


@pytest.mark.asyncio
async def test_a_handle_with_no_context_call_at_all_says_so(monkeypatch, tmp_path):
    class NoBreakdown(FakeEngine):
        context_usage = None

    app, _fake = await _app(monkeypatch, tmp_path, NoBreakdown([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert await _run(app, pilot, "/context") == CONTEXT_UNAVAILABLE


@pytest.mark.asyncio
async def test_a_refusal_is_reported_not_swallowed(monkeypatch, tmp_path):
    fake = FakeEngine([])
    fake.context_usage_error = RuntimeError("session is not connected")
    app, _fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/context")
    assert "session is not connected" in text
    assert "Free space" not in text


def test_a_row_the_cli_did_not_measure_is_omitted_not_zeroed():
    """No number, no row. A category the CLI sent without a token count
    must not render as `0` -- that would be DOXA asserting something it was
    never told."""
    text = context_breakdown_text(context_breakdown({
        "totalTokens": 100, "maxTokens": 1000,
        "categories": [
            {"name": "Measured", "tokens": 100},
            {"name": "Unmeasured", "color": "grey"},
        ],
    }))
    assert "Measured" in text
    assert "Unmeasured" not in text


def test_absent_totals_are_absent_rather_than_defaulted():
    """A reply with no window size gets no window line and no percentages
    -- not a made-up denominator."""
    breakdown = context_breakdown({"categories": [{"name": "Messages", "tokens": 5}]})
    assert "max_tokens" not in breakdown
    assert "percentage" not in breakdown
    text = context_breakdown_text(breakdown)
    assert "of window" not in text
    assert "in use" not in text
    assert "Messages" in text


def test_a_component_the_cli_cannot_separate_is_reported_in_characters():
    """The one figure DOXA contributes. The LORE snapshot is appended to
    the system prompt, so its tokens are inside the CLI's system-prompt
    row and cannot be pulled back out. DOXA reports what it genuinely
    knows -- the exact character count -- and says where the tokens went,
    rather than dividing by four and printing a token number nobody
    measured."""
    text = context_breakdown_text(_breakdown(lore_snapshot_chars=4_812))
    assert "4,812 characters" in text
    assert "4,812 tokens" not in text
    assert "INSIDE the system-" in text


def test_the_session_worktree_block_is_reported_as_its_own_characters():
    """The SECOND thing DOXA may append after the LORE snapshot -- the
    [SESSION WORKTREE] block -- gets its OWN characters line, not folded
    into lore_snapshot_chars: they are separate DOXA-contributed
    components of the one CLI 'system prompt' row, and conflating them
    would make either figure wrong the day only one changes shape."""
    text = context_breakdown_text(
        _breakdown(lore_snapshot_chars=4_812, worktree_notice_chars=210)
    )
    assert "4,812 characters" in text
    assert "210 characters" in text
    assert text.count("INSIDE the system-prompt row") == 2
    assert "worktree notice" in text


def test_a_session_with_no_worktree_gets_no_worktree_notice_line():
    """Hide-at-zero: a breakdown with no worktree_notice_chars at all
    (the overwhelming majority of sessions) prints no such line."""
    text = context_breakdown_text(_breakdown(lore_snapshot_chars=4_812))
    assert "worktree notice" not in text


def test_the_graph_context_block_is_reported_in_its_own_characters():
    """The graph-backed context block (v0.84.0, DOXA_GRAPH_CONTEXT) gets its
    own characters line too -- but with DIFFERENT wording from the two
    connect-time rows above it: it rides the per-turn additionalContext
    path, not the connect-time system prompt, so the CLI's own usage
    figures already count its tokens correctly once that turn's numbers
    come back. Claiming "INSIDE the system-prompt row" for it, like the
    two rows above, would be false."""
    text = context_breakdown_text(_breakdown(graph_context_chars=640))
    assert "640 characters" in text
    assert "graph context" in text
    assert "additionalContext path" in text
    assert "INSIDE the system-prompt row" not in text


def test_a_turn_with_no_graph_context_block_gets_no_graph_context_line():
    """Hide-at-zero, same rule as the worktree notice above."""
    text = context_breakdown_text(_breakdown(lore_snapshot_chars=4_812))
    assert "graph context" not in text


def test_the_no_estimate_promise_is_made_where_the_user_reads_it():
    text = context_breakdown_text(_breakdown())
    assert "the claude CLI's own measurement" in text
    assert "Nothing on this screen is estimated." in text


# -- normalization: one accounting path, one frame ------------------------


def test_the_breakdown_drops_the_pre_rendered_grid_and_fits_a_daemon_frame():
    """gridRows is a pixel-grid duplicate of `categories` and by far the
    largest field. Dropping it is what lets /context cross the daemon
    socket without a pager -- unlike `beliefs` and `pending`, which needed
    one."""
    from doxa.peers import MAX_FRAME_BYTES

    breakdown = _breakdown(lore_snapshot_chars=4_812)
    assert "gridRows" not in breakdown and "grid_rows" not in breakdown
    encoded = json.dumps({"type": "reply", "id": 1, "ok": True,
                          "usage": breakdown}).encode()
    assert len(encoded) < MAX_FRAME_BYTES
    assert len(encoded) < len(json.dumps(CTX_USAGE).encode()) / 4


def test_an_oversized_list_is_capped_and_the_remainder_is_counted():
    """Same honesty rule the belief and pending pagers keep: a truncated
    list SAYS it was truncated."""
    many = {
        "totalTokens": 10, "maxTokens": 100,
        "mcpTools": [
            {"name": f"tool_{n}", "serverName": "srv", "tokens": n}
            for n in range(engine_mod.CONTEXT_ROW_CAP + 7)
        ],
    }
    breakdown = context_breakdown(many)
    assert len(breakdown["mcp_tools"]) == engine_mod.CONTEXT_ROW_CAP
    assert breakdown["mcp_tools_dropped"] == 7
    assert "… and 7 more not shown" in context_breakdown_text(breakdown)


def test_a_reply_with_nothing_in_it_renders_no_empty_headings():
    """A heading with nothing under it is the placeholder row this house
    does not ship (doxa/commands.py's own rule)."""
    text = context_breakdown_text(context_breakdown({"model": "m", "totalTokens": 1,
                                           "maxTokens": 2}))
    assert "memory files" not in text
    assert "mcp tools" not in text


# -- the engine: ONE measurement behind both surfaces ---------------------


@pytest.mark.asyncio
async def test_the_chip_percentage_and_the_breakdown_come_from_one_call(
    tmp_path, monkeypatch
):
    """The reason this was a refactor and not a second code path: through
    v0.34.0 the engine asked for the whole breakdown and kept one float.
    Now it keeps the reply, and the ctx% chip reads it back -- so /context
    and the status bar cannot disagree about the same session."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    factory, created = factory_with_script(
        [ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                       is_error=False, num_turns=1, session_id="s",
                       total_cost_usd=0.0)],
        ctx_usage=CTX_USAGE,
    )
    eng = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await eng.start()
    events = [ev async for ev in eng.send("hi")]
    done = next(e for e in events if e.type == "turn_done")

    assert done.data["ctx_percentage"] == pytest.approx(33.7)
    assert eng.last_ctx_percentage == pytest.approx(33.7)
    assert eng.last_context_usage is CTX_USAGE
    assert eng.usage_summary()["ctx_percentage"] == pytest.approx(33.7)

    breakdown = await eng.context_usage()
    assert breakdown["percentage"] == pytest.approx(33.7)
    assert breakdown["total_tokens"] == 60_650
    await eng.finalize()


@pytest.mark.asyncio
async def test_the_engine_records_the_exact_size_of_the_snapshot_it_injected(
    tmp_path, monkeypatch
):
    """Not an estimate and not a re-read: the number is the length of the
    string this session actually appended to its system prompt."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    factory, created = factory_with_script([], ctx_usage=CTX_USAGE)
    eng = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    assert eng.lore_snapshot_chars is None
    await eng.start()

    appended = created[0].options.system_prompt["append"]
    assert appended.startswith("[LORE SNAPSHOT]\n")
    snapshot = appended[len("[LORE SNAPSHOT]\n"):]
    assert eng.lore_snapshot_chars == len(snapshot)

    breakdown = await eng.context_usage()
    assert breakdown["lore_snapshot_chars"] == len(snapshot)
    await eng.finalize()


@pytest.mark.asyncio
async def test_a_client_that_cannot_be_asked_leaves_no_stale_reading(
    tmp_path, monkeypatch
):
    """A failing control request must clear the chip rather than leaving
    the previous turn's numbers standing -- all THREE of them, since item
    X's absolute pair is now read off the same measurement as the
    percentage. A stale window size is worse than a missing one."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    factory, _created = factory_with_script(
        [ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                       is_error=False, num_turns=1, session_id="s",
                       total_cost_usd=0.0)],
    )  # no ctx_usage scripted -> get_context_usage raises
    eng = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await eng.start()
    eng.last_ctx_percentage = 99.0
    eng.last_ctx_tokens = 12_345
    eng.last_ctx_max_tokens = 200_000
    [ev async for ev in eng.send("hi")]
    assert eng.last_ctx_percentage is None
    assert eng.last_ctx_tokens is None
    assert eng.last_ctx_max_tokens is None
    assert eng.last_context_usage is None
    assert await eng.context_usage() is None
    await eng.finalize()


class _CountingClient:
    """A minimal SDK-client stand-in that COUNTS ``get_context_usage``
    calls -- how many times a turn asks is the whole point of the test
    below, and ``tests.fakes.FakeClient`` does not count."""

    def __init__(self, options, ctx_usage, calls):
        self.options = options
        self._ctx_usage = ctx_usage
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt, session_id="default"):
        return None

    async def receive_response(self):
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="s", total_cost_usd=0.0,
        )

    async def get_context_usage(self):
        self._calls.append(1)
        return self._ctx_usage

    async def get_server_info(self):
        return None


@pytest.mark.asyncio
async def test_the_chip_triple_and_the_breakdown_are_one_measurement(
    tmp_path, monkeypatch
):
    """The v0.35.0 / v0.36.0 reconciliation, pinned.

    Items X and K widened the same engine call independently and to
    different depths -- X to ``(percentage, used, limit)`` for the status
    chip, K to the whole reply for ``/context``. Merged naively that is two
    calls and two caches of one number: exactly the drift both items set
    out to remove, and precisely what would let the chip and the command
    disagree about the same session.

    So there is ONE call and ONE cache. ``_safe_ctx_usage`` reads what
    ``_safe_context_usage`` measured. A counting client proves a finished
    turn asks once, and the chip's three numbers are asserted equal to the
    three the breakdown carries -- including the chip's rendered words."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    calls: list[int] = []
    eng = SessionEngine(
        cwd=str(tmp_path),
        client_factory=lambda options: _CountingClient(options, CTX_USAGE, calls),
    )
    await eng.start()
    [ev async for ev in eng.send("hi")]
    assert len(calls) == 1, "a finished turn asked for the context twice"

    # Item X's triple, straight off item K's cached reply -- no new call.
    assert await eng._safe_ctx_usage() == (33.7, 60_650, 180_000)
    assert len(calls) == 2  # this explicit re-read is the only extra ask
    assert (
        eng.last_ctx_percentage,
        eng.last_ctx_tokens,
        eng.last_ctx_max_tokens,
    ) == (33.7, 60_650, 180_000)

    breakdown = await eng.context_usage()
    assert breakdown["percentage"] == eng.last_ctx_percentage
    assert breakdown["total_tokens"] == eng.last_ctx_tokens
    assert breakdown["max_tokens"] == eng.last_ctx_max_tokens
    # The chip's own words come off the same three numbers.
    assert ctx_text(33.7, 60_650, 180_000, absolute=True) == "ctx 34% 61k/180k"
    await eng.finalize()


def test_the_breakdown_applies_item_x_s_unknown_token_rule():
    """``_as_tokens`` is item X's one honesty rule for a token figure, and
    both surfaces built on this measurement now share it: a non-numeric or
    negative count is UNKNOWN, so the key is absent rather than present as
    a confident zero."""
    breakdown = context_breakdown({
        "totalTokens": "lots", "maxTokens": -1, "rawMaxTokens": True,
        "percentage": 12.0,
    })
    assert "total_tokens" not in breakdown
    assert "max_tokens" not in breakdown
    assert "raw_max_tokens" not in breakdown
    assert breakdown["percentage"] == 12.0
    assert "in use" not in context_breakdown_text(breakdown)


# -- the grid: Claude Code's own look, in place of the v0.75.0 bar --------
#
# "instead of the numbers" is read here exactly as v0.75.0 read it: LEADS
# the numbers, not replaces them. context_breakdown_text (the numbers) is
# untouched by every test above this line, and stays untouched below --
# context_grid_text/context_grid_cells/context_sources_text are additive
# functions nothing else calls yet, spliced in by ContextBlock.render the
# same way context_bar_text used to be.

GRID_WIDTH = GRID_COLUMNS * GRID_CELL_WIDTH  # 30 -- the grid's one true width


def _plain(text: str) -> str:
    """Strip this module's ``[#RRGGBB]...[/]`` color spans back off, the
    same regex ``tests/test_banner.py``'s ``_plain_lines`` uses for the
    drawn boot mark -- the grid is built out of the identical markup
    shape.

    The ascii cell style's own content, a literal ``[#]``/``[ ]``, is
    escaped in production (``doxa.ui.labels._escape_markup``) because it
    is otherwise indistinguishable from a color tag with zero hex digits
    -- exactly the shape the strip regex below matches. A dumb regex does
    not know an escaped ``\\[`` should be left alone, so the escaped
    bracket is hidden behind a sentinel the regex cannot match BEFORE
    stripping, then restored to a plain ``[`` afterward -- the same two-
    step a real Rich/Textual markup parser performs in one."""
    import re

    sentinel = ""
    protected = text.replace("\\[", sentinel)
    stripped = re.sub(r"\[/?#?[0-9A-Fa-f]{0,6}\]", "", protected)
    return stripped.replace(sentinel, "[")


def test_no_reported_window_no_grid_same_rule_as_the_numbers():
    """Item K's central rule, restated for the grid: a limit the CLI never
    sent reads ``?`` and stays ``?`` -- there is no denominator to be
    proportional against, so there is no grid, not one drawn against a
    guessed 200000."""
    breakdown = context_breakdown({
        "totalTokens": 100,
        "categories": [{"name": "Messages", "tokens": 100}],
    })
    assert "max_tokens" not in breakdown
    assert context_grid_cells(breakdown) is None
    assert context_grid_text(breakdown, 120) == ""


def test_no_measured_categories_no_grid():
    """A window size with nothing to divide it into is the other half of
    the same rule: percentage and totals alone are not a shape."""
    breakdown = context_breakdown({"totalTokens": 100, "maxTokens": 1000})
    assert breakdown.get("categories") == []
    assert context_grid_cells(breakdown) is None


def test_a_box_too_narrow_drops_the_grid_not_the_numbers():
    """Below the grid's own fixed width (GRID_COLUMNS * GRID_CELL_WIDTH)
    there is no smaller grid to fall back to -- unlike the old proportional
    bar, this grid never shrinks, so context_grid_text degrades straight
    to "" rather than ship a truncated shape, and context_breakdown_text
    (unexercised by width at all) keeps printing the exact numbers
    regardless."""
    breakdown = _breakdown()
    assert context_grid_text(breakdown, GRID_WIDTH - 1) == ""
    assert context_grid_text(breakdown, GRID_WIDTH) != ""
    assert "60,650 / 180,000" in context_breakdown_text(breakdown)


def test_a_sub_full_cell_component_draws_no_visible_cell():
    """FLOOR, not round, is item K's own small-lie rule pinned in the
    picture as well as the numbers: a category whose cumulative share
    reaches 0.9 of ONE cell's width (not yet a whole cell) must draw ZERO
    cells -- rounding it to the nearest cell (0.9 rounds to 1) would paint
    a filled cell for a component that has not actually earned one.
    200,000-token window, 900-token category: 900/200,000 * 200 cells =
    0.9 -- floor(0.9) == 0, the exact boundary a round-based
    implementation would get wrong."""
    breakdown = context_breakdown({
        "totalTokens": 200_000,
        "maxTokens": 200_000,
        "categories": [
            {"name": "Sliver", "tokens": 900},          # 0.9 of one cell
            {"name": "Messages", "tokens": 99_100},
            {"name": "Free space", "tokens": 100_000},
        ],
    })
    cells = context_grid_cells(breakdown)
    assert cells is not None
    assert len(cells) == GRID_CELLS
    assert all(name != "Sliver" for name, _color in cells), (
        "a 0.9-of-a-cell component painted a full cell"
    )


def test_cells_always_sum_to_exactly_two_hundred():
    """The grid never stretches (unlike the old bar, whose width tracked
    the pane), so its cell total is a CONSTANT -- pinned here against the
    same trip-prone share set the old bar's overflow test used (independent
    per-category rounding overshooting), which the floor-and-clamp
    construction in context_grid_cells cannot reproduce regardless of how
    many categories are in play."""
    shares = [0.15] * 6 + [0.10]
    window = 1_000_000
    breakdown = context_breakdown({
        "totalTokens": window,
        "maxTokens": window,
        "categories": [
            {"name": f"cat{i}", "tokens": int(window * share)}
            for i, share in enumerate(shares)
        ],
    })
    cells = context_grid_cells(breakdown)
    assert cells is not None
    assert len(cells) == GRID_CELLS


def test_free_space_draws_in_the_track_color_not_a_content_color():
    """"Free space" is the CLI's own name for the window's unspent
    remainder (it is what makes ``categories`` sum to ``max_tokens`` in
    the fixture at the top of this file) -- the grid reads it as the empty
    part of the picture, in the same border-grey theme.tcss already uses
    to mean "boundary, not content", never as one more colored component
    competing for attention with what was actually spent. Free space is
    119,350/180,000 of the fixture's window: floor(119350/180000 * 200) =
    133 cells, and since it is also the LAST category in the fixture's own
    list with nothing behind it, those are exactly the grid's trailing 133
    cells."""
    cells = context_grid_cells(_breakdown())
    assert cells is not None
    free = [cell for cell in cells if cell[1] == CONTEXT_GRID_TRACK]
    assert len(free) == 133
    assert all(name == "" for name, _color in free)
    assert cells[-133:] == free


def test_component_colors_are_stable_regardless_of_list_order():
    """A category's color comes from its OWN NAME now, not its position in
    the CLI's list -- unlike the old position-keyed bar palette, the
    grid's legend has to label a color consistently, so the same category
    reported in a different position (an SDK that reorders its own reply)
    must still wear the same color."""
    from doxa.ui.labels import CONTEXT_GRID_CATEGORY_COLORS

    forward = context_breakdown({
        "totalTokens": 100, "maxTokens": 100,
        "categories": [
            {"name": "System prompt", "tokens": 40},
            {"name": "Messages", "tokens": 60},
        ],
    })
    reversed_order = context_breakdown({
        "totalTokens": 100, "maxTokens": 100,
        "categories": [
            {"name": "Messages", "tokens": 60},
            {"name": "System prompt", "tokens": 40},
        ],
    })
    forward_colors = {name: color for name, color in context_grid_cells(forward) if name}
    reversed_colors = {
        name: color for name, color in context_grid_cells(reversed_order) if name
    }
    assert forward_colors["System prompt"] == CONTEXT_GRID_CATEGORY_COLORS["system prompt"]
    assert forward_colors["Messages"] == CONTEXT_GRID_CATEGORY_COLORS["messages"]
    assert forward_colors == reversed_colors


def test_context_grid_mode_reads_the_config_setting(monkeypatch):
    """DOXA cannot probe a terminal's own font coverage -- this is the
    one manual switch, defaulting to glyphs (Claude Code's own look)."""
    monkeypatch.delenv("DOXA_CONTEXT_GRID", raising=False)
    assert context_grid_mode() == "glyphs"
    monkeypatch.setenv("DOXA_CONTEXT_GRID", "ascii")
    assert context_grid_mode() == "ascii"
    monkeypatch.setenv("DOXA_CONTEXT_GRID", "glyphs")
    assert context_grid_mode() == "glyphs"
    monkeypatch.setenv("DOXA_CONTEXT_GRID", "nonsense")
    assert context_grid_mode() == "glyphs"


def test_glyph_style_uses_only_the_three_draughts_glyphs():
    """The exact three code points the owner asked for (U+26C0, U+26C1,
    U+26F6) -- never a full block (the v0.75.0 bar's own glyph) and never
    a Geometric Shapes triangle (the tofu risk v0.58.0's banner work
    rejected)."""
    text = context_grid_text(_breakdown(), 120, mode="glyphs")
    plain = _plain(text)
    assert "⛀" in plain or "⛁" in plain
    assert "⛶" in plain
    assert "█" not in plain
    assert not any(ch in plain for ch in "◢◣◤◥▲▼△▽")


def test_ascii_style_uses_bracket_cells_only():
    """The tofu-proof fallback: universal ASCII, no Miscellaneous Symbols
    at all."""
    text = context_grid_text(_breakdown(), 120, mode="ascii")
    plain = _plain(text)
    assert "⛀" not in plain and "⛁" not in plain and "⛶" not in plain
    grid_rows = [line for line in plain.splitlines() if line.startswith("[")]
    assert grid_rows, "no ascii grid row found"
    assert all(set(row[:GRID_WIDTH]) <= set("[]# ") for row in grid_rows)


def test_ascii_and_glyph_styles_share_the_identical_width_and_geometry():
    """'Width math: solve it once for the wider of the two forms' -- both
    styles occupy exactly GRID_WIDTH columns per row and GRID_ROWS rows,
    so flipping the setting changes only the characters, never the
    layout."""
    glyph_lines = context_grid_text(_breakdown(), GRID_WIDTH, mode="glyphs").splitlines()
    ascii_lines = context_grid_text(_breakdown(), GRID_WIDTH, mode="ascii").splitlines()
    assert len(glyph_lines) == len(ascii_lines) == GRID_ROWS
    for g_line, a_line in zip(glyph_lines, ascii_lines):
        assert len(_plain(g_line)) == len(_plain(a_line)) == GRID_WIDTH


def test_the_side_panel_names_the_model_beside_the_top_rows():
    """Claude Code's own layout: the model's tier word (the same word tab
    labels already use) beside the very first grid row, the raw model id
    beside the second."""
    text = context_grid_text(_breakdown(), 100)
    lines = text.splitlines()
    assert len(lines) == GRID_ROWS
    assert "Opus" in _plain(lines[0])
    assert "claude-opus-4-6" in _plain(lines[1])


def test_the_legend_sits_beside_the_lower_rows_and_never_says_estimated():
    """The category legend, with every category's own tokens and share of
    window -- and NOT Claude Code's own "Estimated usage by category"
    heading, because DOXA does not estimate anything on this screen
    (item K's third honesty rule)."""
    text = context_grid_text(_breakdown(), 100)
    plain = _plain(text)
    assert "Usage by category" in plain
    assert "Estimated" not in plain
    assert "System prompt: 3k tokens" in plain
    assert "(1.8%)" in plain  # 3,200 / 180,000
    assert "Free space" in plain


def test_the_panel_is_omitted_below_its_own_minimum_but_the_grid_still_draws():
    """Too narrow for the side panel to be legible: the grid still draws
    at its one true size, the panel drops out whole rather than truncate
    into noise -- every figure it would have shown is still one screen
    below, in context_breakdown_text, unchanged."""
    from doxa.ui.labels import GRID_GUTTER, GRID_PANEL_MIN_COLUMNS

    too_narrow = GRID_WIDTH + GRID_GUTTER + GRID_PANEL_MIN_COLUMNS - 1
    text = context_grid_text(_breakdown(), too_narrow)
    plain = _plain(text)
    assert len(text.splitlines()) == GRID_ROWS
    assert "Opus" not in plain
    assert "Usage by category" not in plain
    wide_enough = GRID_WIDTH + GRID_GUTTER + GRID_PANEL_MIN_COLUMNS
    assert "Usage by category" in _plain(context_grid_text(_breakdown(), wide_enough))


# -- the per-source sections: MCP tools, agents, adopted-plugin skills ----


def test_context_sources_text_reports_mcp_tools_and_agents():
    """Both are real ``get_context_usage`` fields -- ``mcp_tools`` already
    normalized before this redesign, ``agents`` new to it (see
    doxa.engine.context_breakdown)."""
    text = context_sources_text(_breakdown())
    assert "MCP tools" in text
    assert "doxa_lore: 1 tools · 380 tokens" in text
    assert "Agents" in text
    assert "1 agents · 900 tokens" in text
    assert "Skills" not in text  # hide-at-zero: no adopted_skills in _breakdown()


def test_context_sources_text_reports_adopted_skills_when_present():
    """The one figure in this section DOXA measures itself, because
    get_context_usage has no ``skills`` field at all -- gated on the
    adopt_plugins setting via doxa.claude_plugins.adopted_skill_summary."""
    text = context_sources_text(_breakdown(adopted_skills=5, adopted_skill_plugins=2))
    assert "Skills · adopted plugins" in text
    assert "5 skills from 2 plugins" in text


def test_context_sources_text_empty_when_nothing_to_report():
    breakdown = context_breakdown({"totalTokens": 1, "maxTokens": 2})
    assert context_sources_text(breakdown) == ""
    assert context_sources_text(None) == ""


@pytest.mark.asyncio
async def test_context_leads_with_a_grid_of_draughts_glyphs_and_keeps_the_numbers(
    monkeypatch, tmp_path
):
    """The rendered outcome, measured rather than assumed (the v0.28.0
    rule: assert what actually painted, not that a matching widget
    exists). At 100 columns the grid is real draughts-glyph art, the side
    panel names the model, AND every number /context has always printed
    is still on screen beneath it."""
    fake = FakeEngine([])
    fake.context_usage_result = _breakdown()
    app, _fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "/context"
        await pilot.press("enter")
        block = None
        for _ in range(300):
            found = list(app.query(ContextBlock))
            if found:
                block = found[0]
                # The grid is fitted to MEASURED width, which lands a
                # frame after mount -- later still on a loaded machine.
                # Polling only for the mount made the old bar's version of
                # this test fail under the full suite while passing alone:
                # the first frame is honestly the numbers view, and the
                # grid is the next one. Poll for the grid itself.
                plain = _plain(str(block.renderable))
                if "⛀" in plain or "⛁" in plain:
                    break
            await pilot.pause(0.02)
        assert block is not None, "/context did not mount a ContextBlock"
        assert block.region.height > 0
        rendered = str(block.renderable)
        plain = _plain(rendered)
        assert "⛀" in plain or "⛁" in plain, f"grid never painted; last frame: {plain[:200]!r}"
        assert "⛶" in plain
        assert "Opus" in plain
        assert "Usage by category" in plain
        # The numbers underneath are exactly what they always were.
        assert "60,650 / 180,000" in rendered
        assert "33.7%" in rendered
        assert "System prompt" in rendered
        assert "Free space" in rendered


@pytest.mark.asyncio
async def test_the_grid_never_overflows_the_content_box_it_was_measured_against(
    monkeypatch, tmp_path
):
    """v0.70.0's own precedent (the boot mark overflowing a column a
    scrollbar had since narrowed) is exactly the failure this pins for
    ``/context``: at every width tried, no rendered row is wider than
    ``content_size.width`` -- fit against the widget's OWN measured box,
    never against ``columns``."""
    for width in (20, 30, 40, 60, 80, 120):
        fake = FakeEngine([])
        fake.context_usage_result = _breakdown()
        app, _fake = await _app(monkeypatch, tmp_path / f"w{width}", fake)
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause()
            app.query_one("#prompt-input").value = "/context"
            await pilot.press("enter")
            block = None
            for _ in range(300):
                found = list(app.query(ContextBlock))
                if found:
                    block = found[0]
                    break
                await pilot.pause(0.02)
            assert block is not None
            available = block.content_size.width
            rendered = _plain(str(block.renderable))
            for line in rendered.splitlines():
                if "⛀" not in line and "⛁" not in line and "⛶" not in line:
                    continue
                assert len(line) <= available, (
                    f"at width {width} (content {available}) the grid row "
                    f"{line!r} ({len(line)} cells) overflows"
                )
            if available < GRID_WIDTH:
                assert "⛀" not in rendered and "⛁" not in rendered, (
                    f"width {width} (content {available}) drew a grid below "
                    "its own fixed width"
                )


@pytest.mark.asyncio
async def test_a_narrow_pane_drops_the_grid_and_keeps_the_numbers(
    monkeypatch, tmp_path
):
    """The degrade path named in the brief: too narrow for the grid's own
    fixed shape, the numbers stay -- /context never goes silent just
    because the grid could not."""
    fake = FakeEngine([])
    fake.context_usage_result = _breakdown()
    app, _fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(24, 24)) as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "/context"
        await pilot.press("enter")
        block = None
        for _ in range(300):
            found = list(app.query(ContextBlock))
            if found:
                block = found[0]
                break
            await pilot.pause(0.02)
        assert block is not None
        assert block.content_size.width < GRID_WIDTH
        rendered = str(block.renderable)
        assert "⛀" not in rendered and "⛁" not in rendered and "⛶" not in rendered
        assert "60,650" in rendered


@pytest.mark.asyncio
async def test_the_ascii_setting_swaps_glyphs_for_brackets_in_the_live_widget(
    monkeypatch, tmp_path
):
    """The setting end to end: DOXA_CONTEXT_GRID=ascii reaches the actual
    mounted widget, not just the pure formatter."""
    monkeypatch.setenv("DOXA_CONTEXT_GRID", "ascii")
    fake = FakeEngine([])
    fake.context_usage_result = _breakdown()
    app, _fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "/context"
        await pilot.press("enter")
        block = None
        for _ in range(300):
            found = list(app.query(ContextBlock))
            if found:
                block = found[0]
                plain = _plain(str(block.renderable))
                if "[#]" in plain or "[ ]" in plain:
                    break
            await pilot.pause(0.02)
        assert block is not None
        plain = _plain(str(block.renderable))
        assert "[#]" in plain or "[ ]" in plain
        assert "⛀" not in plain and "⛁" not in plain and "⛶" not in plain

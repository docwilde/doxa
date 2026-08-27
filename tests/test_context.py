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
    CONTEXT_BAR_MAX_COLUMNS,
    CONTEXT_BAR_MIN_COLUMNS,
    CONTEXT_BAR_TRACK,
    CONTEXT_UNAVAILABLE,
    context_bar_segments,
    context_bar_text,
    context_breakdown_text,
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


# -- the bar: block art in place of the numbers, numbers still below ------
#
# "instead of the numbers" is read here as LEADS the numbers, not replaces
# them: a bar of ``█`` can show the SHAPE of the window at a glance but
# cannot tell 4% from 6%, and item K's whole premise is that every figure
# on this screen stays a reachable measurement. context_breakdown_text
# (the numbers) is untouched by every test above this line -- deliberately,
# since context_bar_text/context_bar_segments are new, additive functions
# nothing else calls yet.


def _plain(text: str) -> str:
    """Strip this module's ``[#RRGGBB]...[/]`` color spans back off, the
    same regex ``tests/test_banner.py``'s ``_plain_lines`` uses for the
    drawn boot mark -- the bar is built out of the identical markup
    shape."""
    import re

    return re.sub(r"\[/?#?[0-9A-Fa-f]{0,6}\]", "", text)


def test_no_reported_window_no_bar_same_rule_as_the_numbers():
    """Item K's central rule, restated for the bar: a limit the CLI never
    sent reads ``?`` and stays ``?`` -- there is no denominator to be
    proportional against, so there is no bar, not one drawn against a
    guessed 200000."""
    breakdown = context_breakdown({
        "totalTokens": 100,
        "categories": [{"name": "Messages", "tokens": 100}],
    })
    assert "max_tokens" not in breakdown
    assert context_bar_segments(breakdown, 80) is None
    assert context_bar_text(breakdown, 80) == ""


def test_no_measured_categories_no_bar():
    """A window size with nothing to divide it into is the other half of
    the same rule: percentage and totals alone are not a shape."""
    breakdown = context_breakdown({"totalTokens": 100, "maxTokens": 1000})
    assert breakdown.get("categories") == []
    assert context_bar_segments(breakdown, 80) is None


def test_a_box_too_narrow_drops_the_bar_not_the_numbers():
    """Below CONTEXT_BAR_MIN_COLUMNS the bar is 2-3 blocks of noise, not a
    shape -- context_bar_text degrades to "" rather than ship it, and
    context_breakdown_text (unexercised by width at all) keeps printing
    the exact numbers regardless."""
    breakdown = _breakdown()
    assert context_bar_segments(breakdown, CONTEXT_BAR_MIN_COLUMNS - 1) is None
    assert context_bar_text(breakdown, CONTEXT_BAR_MIN_COLUMNS - 1) == ""
    assert context_bar_segments(breakdown, CONTEXT_BAR_MIN_COLUMNS) is not None
    assert "60,650 / 180,000" in context_breakdown_text(breakdown)


def test_a_sub_half_block_component_draws_no_visible_sliver():
    """Rounding a 0.2% component up to one block is a lie at the small
    end. A category worth 0.2% of a 60-wide bar (0.12 of a block) must
    draw ZERO blocks, and every category ahead of it in the list must not
    be inflated to cover for it -- the bar's own width still has to sum
    correctly with the sliver gone."""
    breakdown = context_breakdown({
        "totalTokens": 100_000,
        "maxTokens": 100_000,
        "categories": [
            {"name": "Sliver", "tokens": 200},        # 0.2% of the window
            {"name": "Messages", "tokens": 49_800},   # 49.8%
            {"name": "Free space", "tokens": 50_000},  # 50%
        ],
    })
    segments = context_bar_segments(breakdown, 60)
    assert segments is not None
    colors = [color for color, _count in segments]
    # The sliver's own palette slot (index 0) never appears -- it drew
    # zero blocks and was skipped outright, not folded into a neighbor.
    from doxa.ui.labels import CONTEXT_BAR_PALETTE

    assert CONTEXT_BAR_PALETTE[0] not in colors
    assert sum(count for _color, count in segments) == 60


def test_segments_never_exceed_the_width_they_were_given():
    """The v0.70.0 lesson, restated for the bar: independent per-category
    rounding can overshoot (several categories each just over a half-block
    boundary, each rounding up) -- the exact failure shape that let the
    boot mark's own fit overflow its column once already. Pinned here
    against a set of shares chosen to trip that: six categories at 15%
    apiece (a share whose ``* width`` lands past the halfway mark at every
    width tried) plus one at 10%, for widths spanning
    CONTEXT_BAR_MIN_COLUMNS to CONTEXT_BAR_MAX_COLUMNS and a few points
    past it."""
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
    for width in (
        CONTEXT_BAR_MIN_COLUMNS,
        30,
        40,
        CONTEXT_BAR_MAX_COLUMNS,
        CONTEXT_BAR_MAX_COLUMNS + 40,
    ):
        segments = context_bar_segments(breakdown, width)
        assert segments is not None
        total = sum(count for _color, count in segments)
        expected = min(width, CONTEXT_BAR_MAX_COLUMNS)
        assert total == expected, f"width {width}: bar drew {total}, box is {expected}"


def test_free_space_draws_in_the_track_color_not_a_content_color():
    """"Free space" is the CLI's own name for the window's unspent
    remainder (it is what makes ``categories`` sum to ``max_tokens`` in
    the fixture at the top of this file) -- the bar reads it as the empty
    part of the picture, in the same border-grey theme.tcss already uses
    to mean "boundary, not content", never as one more colored component
    competing for attention with what was actually spent."""
    segments = context_bar_segments(_breakdown(), 60)
    assert segments is not None
    assert segments[-1] == (CONTEXT_BAR_TRACK, 40)  # Free space is 119_350/180_000


def test_component_colors_are_stable_across_widths():
    """A category's color comes from its OWN position in the CLI's list,
    not from how many segments happened to draw a block at this
    particular width -- otherwise a category that rounds to zero at 24
    columns would silently push every later category into a different
    hue than it wore at 60, which is exactly the kind of thing "the same
    component, the same color" is supposed to rule out."""
    from doxa.ui.labels import CONTEXT_BAR_PALETTE

    breakdown = _breakdown()
    wide = dict(context_bar_segments(breakdown, 60))
    narrow = context_bar_segments(breakdown, CONTEXT_BAR_MIN_COLUMNS)
    assert narrow is not None
    # System tools (idx 1, the second-largest real category) survives at
    # both widths and must keep the same color at both.
    assert CONTEXT_BAR_PALETTE[1] in wide
    assert CONTEXT_BAR_PALETTE[1] in dict(narrow)


def test_context_bar_text_repeats_the_percentage_next_to_the_picture():
    """A bar with no number beside it invites reading 4% as 6% -- the one
    figure that belongs on the SAME line as a picture is the figure the
    picture is a picture of, at the numbers view's own one-decimal
    precision so the two never read as disagreeing over rounding."""
    text = context_bar_text(_breakdown(), 60)
    assert text.endswith("33.7%")
    assert "█" in _plain(text)


@pytest.mark.asyncio
async def test_context_leads_with_a_bar_of_full_blocks_and_keeps_the_numbers(
    monkeypatch, tmp_path
):
    """The rendered outcome, measured rather than assumed (the v0.28.0
    rule: assert what actually painted, not that a matching widget
    exists). At 80 columns the bar is real block art AND every number
    /context has always printed is still on screen beneath it."""
    fake = FakeEngine([])
    fake.context_usage_result = _breakdown()
    app, _fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "/context"
        await pilot.press("enter")
        block = None
        for _ in range(300):
            found = list(app.query(ContextBlock))
            if found:
                block = found[0]
                # The bar is fitted to MEASURED width, which lands a frame
                # after mount -- later still on a loaded machine. Polling
                # only for the mount made this fail under the full suite
                # while passing alone: the first frame is honestly the
                # numbers view, and the bar is the next one. Poll for the
                # bar itself.
                if "█" in _plain(str(block.renderable)):
                    break
            await pilot.pause(0.02)
        assert block is not None, "/context did not mount a ContextBlock"
        assert block.region.height > 0
        rendered = str(block.renderable)
        plain = _plain(rendered)
        assert "█" in plain, f"bar never painted; last frame: {plain[:120]!r}"
        # The bar is only ONE glyph -- nothing but block art and spaces on
        # its own line, never a half-block or a Geometric Shapes triangle.
        bar_lines = [line for line in plain.splitlines() if "█" in line]
        assert bar_lines, "no rendered line carries the bar"
        stripped_glyphs = set(bar_lines[0].replace(" ", "").replace("%", ""))
        assert stripped_glyphs <= set("█0123456789.")
        # The numbers underneath are exactly what they always were.
        assert "60,650 / 180,000" in rendered
        assert "33.7%" in rendered
        assert "System prompt" in rendered
        assert "Free space" in rendered


@pytest.mark.asyncio
async def test_the_bar_never_overflows_the_content_box_it_was_measured_against(
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
        async with app.run_test(size=(width, 24)) as pilot:
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
                if "█" not in line:
                    continue
                assert len(line) <= available, (
                    f"at width {width} (content {available}) the bar row "
                    f"{line!r} ({len(line)} cells) overflows"
                )
            if available < CONTEXT_BAR_MIN_COLUMNS:
                assert "█" not in rendered, (
                    f"width {width} (content {available}) drew a bar below "
                    "CONTEXT_BAR_MIN_COLUMNS"
                )


@pytest.mark.asyncio
async def test_a_narrow_pane_drops_the_bar_and_keeps_the_numbers(
    monkeypatch, tmp_path
):
    """The degrade path named in the brief: too narrow for a shape, the
    numbers stay -- /context never goes silent just because the bar
    could not."""
    fake = FakeEngine([])
    fake.context_usage_result = _breakdown()
    app, _fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(30, 24)) as pilot:
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
        assert block.content_size.width < CONTEXT_BAR_MIN_COLUMNS
        rendered = str(block.renderable)
        assert "█" not in rendered
        assert "60,650" in rendered

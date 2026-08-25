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
from doxa.app import DoxaApp, SlashComplete, SystemBlock
from doxa.engine import SessionEngine, context_breakdown
from doxa.ui.labels import (
    CONTEXT_UNAVAILABLE,
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

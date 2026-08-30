# SPDX-License-Identifier: AGPL-3.0-only
"""Item X (ctx absolute): the context chip could only ever say a
PERCENTAGE, and 12% of a 200k window is not 12% of a 1M one -- DOXA drives
several models, so the number the chip showed could not answer the only
question anyone asks it ("how much room is actually left?").

What this file pins, in the order the numbers travel:

1. the ENGINE reads used/limit out of the SAME ``get_context_usage()``
   reply the percentage already came from -- one call, one accounting
   path, and an absent limit stays absent rather than becoming 200000;
2. the daemon/client pair carries all three across the socket, so a
   detached session's status bar is not poorer than an in-process one;
3. the CHIP shows the percentage exactly as before by default, and its
   TOOLTIP carries the absolute figures unconditionally -- that tooltip
   is item X's actual guarantee, because it costs no width;
4. the opt-in inline segment (``DOXA_CTX_ABSOLUTE``) appears when asked
   for AND when the terminal can afford it, and is dropped -- not
   truncated, not allowed to push other chips off the row -- when it
   cannot;
5. ``/usage`` prints the exact figures, and says so when the window size
   was never reported.

The UI half asserts what is on SCREEN (the status bar's markup-stripped
text, the tooltip the bar hands back for a hover coordinate), never merely
that a StatusChip object was constructed -- same bar v0.28.0's
invisible-button defect set.
"""

from __future__ import annotations

import pytest

from doxa.session.chips import _ctx_tooltip_absolute
from doxa.ui.labels import (
    CTX_ABSOLUTE_MIN_COLS,
    CTX_RED,
    ctx_absolute_text,
    ctx_chip,
    ctx_text,
    fmt_tokens,
)
from tests.fakes import FakeEngine
from tests.helpers import _chip_offset


# -- the formatters ------------------------------------------------------


def test_fmt_tokens_rounds_and_never_invents_a_zero():
    assert fmt_tokens(0) == "0"
    assert fmt_tokens(812) == "812"
    assert fmt_tokens(24_000) == "24k"
    assert fmt_tokens(199_500) == "200k"
    assert fmt_tokens(1_200_000) == "1.2M"
    # An unmeasured count is an em-dash, NOT "0": "0 tokens used" is a
    # confident statement about something nobody measured.
    assert fmt_tokens(None) == "—"


def test_absolute_segment_says_unknown_instead_of_guessing_a_window():
    assert ctx_absolute_text(24_000, 200_000) == "24k/200k"
    # The limit is the one number DOXA must never make up: the Models API
    # is unreachable under OAuth-only auth (measured in this project), so
    # there is no second source and a hardcoded 200000 would be fiction.
    assert ctx_absolute_text(24_000, None) == "24k/?"
    assert ctx_absolute_text(None, None) is None


def test_ctx_chip_default_is_byte_identical_to_the_percentage_only_chip():
    """The chip's default width cost is unchanged -- the absolute numbers
    are opt-in precisely because this row is the most contended in the
    app."""
    assert ctx_chip(12.0) == "ctx 12%"
    assert ctx_chip(12.0, 24_000, 200_000) == "ctx 12%"
    assert ctx_chip(None) == "ctx —"


def test_ctx_chip_absolute_rides_inside_the_pressure_color():
    """One chip, one escalation: the absolute segment is inside the color
    span, so a red chip is red all the way across rather than half-red."""
    assert ctx_chip(12.0, 24_000, 200_000, absolute=True) == "ctx 12% 24k/200k"
    red = ctx_chip(93.0, 186_000, 200_000, absolute=True)
    assert red == f"[{CTX_RED}]ctx 93% 186k/200k[/]"
    # Plain text and markup are built by one function, so they cannot
    # disagree about the words.
    assert ctx_text(93.0, 186_000, 200_000, absolute=True) in red


def test_tooltip_states_used_limit_and_headroom_in_full():
    hint = _ctx_tooltip_absolute(24_000, 200_000)
    assert "24,000" in hint and "200,000" in hint and "176,000" in hint
    # An unreported window says so; it does not borrow a number.
    unknown = _ctx_tooltip_absolute(24_000, None)
    assert "24,000" in unknown and "200,000" not in unknown
    assert "not something this session's CLI reported" in unknown
    assert "has not reported" in _ctx_tooltip_absolute(None, None)


# -- the engine reads all three from ONE call ---------------------------


@pytest.mark.asyncio
async def test_engine_reads_used_and_limit_from_the_same_usage_reply(tmp_path):
    """No second accounting path: ``totalTokens``/``maxTokens`` come back
    in the very reply ``percentage`` does, so the three can never
    disagree, and the turn_done event carries all of them."""
    from claude_agent_sdk import ResultMessage

    from doxa.engine import SessionEngine
    from tests.fakes import factory_with_script

    factory, created = factory_with_script(
        [ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        )],
        ctx_usage={
            "percentage": 12.5, "totalTokens": 25_000, "maxTokens": 200_000,
        },
    )
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    events = [ev async for ev in engine.send("hi")]

    done = next(e for e in events if e.type == "turn_done")
    assert done.data["ctx_percentage"] == pytest.approx(12.5)
    assert done.data["ctx_tokens"] == 25_000
    assert done.data["ctx_max_tokens"] == 200_000
    assert engine.last_ctx_tokens == 25_000
    assert engine.last_ctx_max_tokens == 200_000
    assert engine.usage_summary()["ctx_max_tokens"] == 200_000
    # ONE call for all three -- the whole point of widening the existing
    # reader instead of adding a second one.
    assert created[0].ctx_usage["maxTokens"] == 200_000


@pytest.mark.asyncio
async def test_engine_leaves_an_unreported_limit_unknown(tmp_path):
    """A reply with no ``maxTokens`` yields None, not a default window."""
    from claude_agent_sdk import ResultMessage

    from doxa.engine import SessionEngine
    from tests.fakes import factory_with_script

    factory, _created = factory_with_script(
        [ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        )],
        ctx_usage={"percentage": 40.0, "totalTokens": 80_000},
    )
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    [ev async for ev in engine.send("hi")]
    assert engine.last_ctx_tokens == 80_000
    assert engine.last_ctx_max_tokens is None


@pytest.mark.asyncio
async def test_absolute_numbers_survive_the_daemon_socket(tmp_path, monkeypatch):
    """Engine/client parity, end to end over a real socket: a DETACHED
    session's status bar must not be poorer than an in-process one, so the
    turn_done frame AND the status reply both carry all three fields and
    EngineClient caches them under the same names SessionEngine uses."""
    import asyncio
    import contextlib

    from claude_agent_sdk import ResultMessage

    from doxa.client import EngineClient
    from doxa.daemon import SessionDaemon
    from doxa.engine import SessionEngine
    from tests.fakes import factory_with_script

    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    factory, _created = factory_with_script(
        [ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        )],
        ctx_usage={
            "percentage": 55.0, "totalTokens": 110_000, "maxTokens": 200_000,
        },
    )
    daemon = SessionDaemon(
        cwd=str(tmp_path), linger_secs=30.0,
        engine_factory=lambda cwd, sid, dsock: SessionEngine(
            cwd=cwd, session_id=sid, client_factory=factory, daemon_socket=dsock,
        ),
    )
    serve_task = asyncio.create_task(daemon.serve())
    await asyncio.wait_for(daemon.ready.wait(), 10)
    try:
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        [ev async for ev in client.send("hi")]
        assert client.last_ctx_tokens == 110_000
        assert client.last_ctx_max_tokens == 200_000
        # And a fresh attach gets them from the STATUS reply alone, with no
        # turn of its own to ride on -- which is what a reattaching tab is.
        second = EngineClient(str(daemon.socket_path))
        await second.start()
        assert second.last_ctx_tokens == 110_000
        assert second.last_ctx_max_tokens == 200_000
        await second.finalize()
    finally:
        if not serve_task.done():
            with contextlib.suppress(Exception):
                await daemon._shutdown("test teardown")
                await asyncio.wait_for(serve_task, 5)


# -- what is actually on screen ------------------------------------------
#
# Same headless-Pilot harness as tests/test_status_chips.py: the assertions
# below read the status bar's markup-STRIPPED text and the tooltip the bar
# hands back for a real hover coordinate, because a StatusChip that was
# merely constructed is exactly what the v0.28.0 invisible-button defect
# passed every test for.


async def _app(monkeypatch, cwd, fake):
    from doxa.app import DoxaApp

    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(cwd) + "-rt")
    engines: list[FakeEngine] = []

    def make():
        engines.append(fake)
        return engines[-1]

    app = DoxaApp(
        cwd=str(cwd), engine_factory=make, new_session_factory=make,
        new_session_factory_at=lambda path: make(),
    )
    return app, engines


def _status_plain(app) -> str:
    from textual.content import Content

    return Content.from_markup(str(app.query_one("#status-bar").renderable)).plain


async def _wait_status(pilot, app, needle: str, tries=200) -> bool:
    for _ in range(tries):
        if needle in _status_plain(app):
            return True
        await pilot.pause(0.02)
    return needle in _status_plain(app)


def _measured_engine(pct=42.0, used=24_000, limit=200_000):
    fake = FakeEngine([])
    fake.last_ctx_percentage = pct
    fake.last_ctx_tokens = used
    fake.last_ctx_max_tokens = limit
    return fake


def _tooltip_over(app, needle: str) -> "str | None":
    """The tooltip the status bar serves for a hover landing on `needle` --
    resolved through :func:`tests.helpers._chip_offset`, which walks the
    bar's own per-chip hint list rather than a bare
    ``_status_plain(app).index(needle)``. Outside a git repo the bar also
    carries a `dir <cwd name>` chip (GitLine.folder_label), and under
    pytest `<cwd name>` IS this test's own name -- a plain `.index()`
    lookup for a short needle like "ctx" can land INSIDE that chip's own
    text instead of the real ctx chip, which is exactly what made this
    helper report the wrong tooltip (or none) for tests whose own name
    happens to contain the needle."""
    from doxa.app import StatusBar

    bar = app.query_one("#status-bar", StatusBar)
    x, _y = _chip_offset(app, needle)
    return bar._tooltip_for_x(x)


@pytest.mark.asyncio
async def test_hovering_the_ctx_chip_states_used_and_total_tokens(
    monkeypatch, tmp_path,
):
    """Item X's actual guarantee: the numbers are reachable with NO width
    spent at all -- default settings, default chip text, and the answer to
    "12% of what?" one hover away."""
    fake = _measured_engine()
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(120, 40)) as pilot:
        # "ctx 42%", not the bare "ctx" -- _measured_engine()'s default
        # percentage, and the chip's own full text (see _chip_offset).
        assert await _wait_status(pilot, app, "ctx 42%")
        # Default: the chip's own width cost is unchanged.
        assert "24k/200k" not in _status_plain(app)
        hint = _tooltip_over(app, "ctx 42%")
        assert hint is not None, "the ctx chip serves no tooltip at all"
        assert "24,000" in hint and "200,000" in hint
        assert "176,000" in hint


@pytest.mark.asyncio
async def test_the_ctx_tooltip_survives_the_red_pressure_tier(
    monkeypatch, tmp_path,
):
    """The tooltip is looked up by finding the chip's text inside the bar's
    markup-STRIPPED string, so a chip keyed by its COLORED markup could
    never match -- the ctx hint used to vanish at exactly the amber and red
    tiers, which is where a reader most wants to know how many tokens are
    left."""
    fake = _measured_engine(pct=93.0, used=186_000)
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(120, 40)) as pilot:
        assert await _wait_status(pilot, app, "ctx 93%")
        hint = _tooltip_over(app, "ctx 93%")
        assert hint is not None, "the red ctx chip serves no tooltip"
        assert "186,000" in hint and "200,000" in hint


@pytest.mark.asyncio
async def test_the_inline_segment_is_opt_in_and_paints_when_asked_for(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("DOXA_CTX_ABSOLUTE", "1")
    fake = _measured_engine()
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(120, 40)) as pilot:
        assert await _wait_status(pilot, app, "24k/200k")
        plain = _status_plain(app)
        assert "ctx 42% 24k/200k" in plain
        # Still one chip, still in the same slot in paint order: the chips
        # to its right are all still on the bar.
        assert plain.index("ctx 42%") < plain.index("beliefs")


@pytest.mark.asyncio
async def test_a_narrow_terminal_drops_the_inline_segment_not_its_neighbours(
    monkeypatch, tmp_path,
):
    """The graceful-degradation requirement, measured. An 80-column
    terminal keeps the percentage and every chip that follows it; the
    convenience segment is what gives way, because the status bar does not
    scroll -- overflow pushes chips off the end of the row."""
    monkeypatch.setenv("DOXA_CTX_ABSOLUTE", "1")
    fake = _measured_engine()
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(80, 24)) as pilot:
        assert await _wait_status(pilot, app, "ctx 42%")
        plain = _status_plain(app)
        assert "24k/200k" not in plain
        assert "beliefs" in plain
        # And the numbers are still reachable, which is why dropping the
        # segment costs nothing that matters.
        assert "24,000" in (_tooltip_over(app, "ctx 42%") or "")
    assert CTX_ABSOLUTE_MIN_COLS > 80


@pytest.mark.asyncio
async def test_usage_prints_the_exact_figures_and_names_an_unknown_window(
    monkeypatch, tmp_path,
):
    fake = _measured_engine()
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.active_pane
        for _ in range(200):
            if pane.engine is not None:
                break
            await pilot.pause(0.02)
        text = pane._usage_text()
        assert "24,000 / 200,000 tokens" in text

        fake.last_ctx_max_tokens = None
        unknown = pane._usage_text()
        assert "24,000 tokens (window size not reported)" in unknown
        assert "200,000" not in unknown

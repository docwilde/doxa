# SPDX-License-Identifier: AGPL-3.0-only
"""Every caller handles a `supports()` of False (v1.4.0).

The spec's third worry, in its own words: "capability is not uniform, and
pretending otherwise is the trap ... a capability map that lies is worse
than no second engine". These tests are the map being told the truth and
the surfaces obeying it -- the ctx chip, the cost chip, the mode chip, the
beliefs picker, ``/context``, ``/mode`` and ``/usage``.

The pairing matters as much as the assertions: every "hidden on codex"
test has a "still there on claude" twin, because the failure mode of a
capability gate is not that it hides too little.
"""

from __future__ import annotations

import pytest
from textual.content import Content

from doxa.app import DoxaApp, SystemBlock
from doxa.codex import CODEX_CAPABILITIES
from tests.fakes import FakeEngine


def _status_plain(app) -> str:
    return Content.from_markup(str(app.query_one("#status-bar").renderable)).plain


def _chip_actions(app) -> "set[str]":
    from tests.helpers import _chip_actions as actions

    return actions(app)


def _codex_fake(**kwargs) -> FakeEngine:
    fake = FakeEngine([], model="gpt-5.4", **kwargs)
    fake.engine_capabilities = CODEX_CAPABILITIES
    # What a Codex session actually reports: real tokens, no window, no
    # dollars. The fake's own usage_summary is overridden the same way the
    # engine's is, so /usage exercises the real absence rather than a
    # zeroed stand-in.
    fake.usage_summary = lambda: {
        "session_id": "codex-session",
        "model": fake.model,
        "num_turns": 1,
        "total_cost_usd": None,
        "ctx_percentage": None,
        "ctx_tokens": None,
        "ctx_max_tokens": None,
        "input_tokens": 70909,
        "output_tokens": 804,
    }
    return fake


async def _app(monkeypatch, cwd, fake):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(cwd) + "-rt")

    def make():
        return fake

    return DoxaApp(cwd=str(cwd), engine_factory=make, new_session_factory=make)


async def _settled(pilot, app, tries=200):
    for _ in range(tries):
        if _status_plain(app).strip():
            return True
        await pilot.pause(0.02)
    return bool(_status_plain(app).strip())


def _system_texts(app) -> "list[str]":
    return [b.text for b in app.query(SystemBlock) if b.id != "identity-block"]


# -- the ctx chip --------------------------------------------------------


@pytest.mark.asyncio
async def test_ctx_chip_is_absent_when_the_engine_has_no_window(monkeypatch, tmp_path):
    """`ctx —` is the right paint for "not measured yet". It is the wrong
    paint for "never will be", where it sits there forever reading as a
    pending measurement."""
    app = await _app(monkeypatch, tmp_path, _codex_fake())
    async with app.run_test() as pilot:
        assert await _settled(pilot, app)
        assert "compact_now" not in _chip_actions(app)
        assert "ctx " not in _status_plain(app)


@pytest.mark.asyncio
async def test_ctx_chip_is_still_there_for_an_engine_that_reports_one(
    monkeypatch, tmp_path,
):
    app = await _app(monkeypatch, tmp_path, FakeEngine([]))
    async with app.run_test() as pilot:
        assert await _settled(pilot, app)
        assert "compact_now" in _chip_actions(app)


# -- the cost chip -------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_chip_is_absent_when_no_cost_was_reported(monkeypatch, tmp_path):
    """`$0.0000` is not the absence of a cost, it is the claim that the
    session was free."""
    app = await _app(monkeypatch, tmp_path, _codex_fake())
    async with app.run_test() as pilot:
        assert await _settled(pilot, app)
        assert "$" not in _status_plain(app)


@pytest.mark.asyncio
async def test_cost_chip_is_still_there_for_claude(monkeypatch, tmp_path):
    app = await _app(monkeypatch, tmp_path, FakeEngine([]))
    async with app.run_test() as pilot:
        assert await _settled(pilot, app)
        assert "$0.0000" in _status_plain(app)


# -- the permission-mode chip and picker --------------------------------


@pytest.mark.asyncio
async def test_mode_chip_is_absent_on_an_engine_without_permission_modes(
    monkeypatch, tmp_path,
):
    app = await _app(
        monkeypatch, tmp_path, _codex_fake(permission_mode="acceptEdits"),
    )
    async with app.run_test() as pilot:
        assert await _settled(pilot, app)
        assert "open_mode_picker" not in _chip_actions(app)


@pytest.mark.asyncio
async def test_mode_chip_is_still_there_for_claude(monkeypatch, tmp_path):
    app = await _app(
        monkeypatch, tmp_path, FakeEngine([], permission_mode="acceptEdits"),
    )
    async with app.run_test() as pilot:
        assert await _settled(pilot, app)
        assert "open_mode_picker" in _chip_actions(app)


@pytest.mark.asyncio
async def test_the_mode_picker_refuses_rather_than_offering_dead_rows(
    monkeypatch, tmp_path,
):
    """The chip is not painted, but the palette can still reach the
    picker's coroutine."""
    app = await _app(monkeypatch, tmp_path, _codex_fake())
    async with app.run_test() as pilot:
        assert await _settled(pilot, app)
        await app.active_pane.open_mode_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker")
        assert not picker.is_open


# -- the beliefs chip ----------------------------------------------------


@pytest.mark.asyncio
async def test_belief_count_shows_but_the_picker_is_not_offered(monkeypatch, tmp_path):
    """The belief store is the project's, not the engine's, so the count
    is real on every engine -- but the picker lives on SessionEngine, and
    a clickable chip that opens nothing would be the lie."""
    app = await _app(monkeypatch, tmp_path, _codex_fake())
    async with app.run_test() as pilot:
        assert await _settled(pilot, app)
        assert "3 beliefs" in _status_plain(app)
        assert "open_beliefs_picker" not in _chip_actions(app)


@pytest.mark.asyncio
async def test_the_beliefs_picker_is_still_offered_on_claude(monkeypatch, tmp_path):
    app = await _app(monkeypatch, tmp_path, FakeEngine([]))
    async with app.run_test() as pilot:
        assert await _settled(pilot, app)
        assert "open_beliefs_picker" in _chip_actions(app)


# -- the commands --------------------------------------------------------


@pytest.mark.asyncio
async def test_context_command_says_so_rather_than_inventing_a_breakdown(
    monkeypatch, tmp_path,
):
    fake = _codex_fake()
    fake.context_usage_result = None
    app = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _settled(pilot, app)
        await app.active_pane._cmd_context("")
        await pilot.pause()
        assert any("context" in text.lower() for text in _system_texts(app))
        # And no ContextBlock was mounted: an absent breakdown is reported,
        # never drawn as an empty grid.
        from doxa.ui.transcript import ContextBlock

        assert not app.query(ContextBlock)


@pytest.mark.asyncio
async def test_mode_command_names_the_reason(monkeypatch, tmp_path):
    app = await _app(monkeypatch, tmp_path, _codex_fake())
    async with app.run_test() as pilot:
        assert await _settled(pilot, app)
        await app.active_pane._cmd_mode("")
        await pilot.pause()
        said = "\n".join(_system_texts(app))
        assert "no permission modes" in said
        # It did NOT list six modes it cannot enter.
        assert "acceptEdits" not in said


@pytest.mark.asyncio
async def test_usage_says_the_cost_is_unreported_not_zero(monkeypatch, tmp_path):
    app = await _app(monkeypatch, tmp_path, _codex_fake())
    async with app.run_test() as pilot:
        assert await _settled(pilot, app)
        await app.active_pane._cmd_usage("")
        await pilot.pause()
        said = "\n".join(_system_texts(app))
        assert "not reported by this engine" in said
        assert "$0.0000" not in said
        # The tokens it DOES report are still there: token accounting and
        # window accounting are different facts.
        assert "70,909" in said


@pytest.mark.asyncio
async def test_usage_still_prints_a_dollar_figure_for_claude(monkeypatch, tmp_path):
    app = await _app(monkeypatch, tmp_path, FakeEngine([]))
    async with app.run_test() as pilot:
        assert await _settled(pilot, app)
        await app.active_pane._cmd_usage("")
        await pilot.pause()
        said = "\n".join(_system_texts(app))
        assert "$0.0000" in said
        assert "not reported by this engine" not in said

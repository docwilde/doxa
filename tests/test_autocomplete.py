"""Slash autocomplete: the prompt's dropdown, driven by the ONE registry.

Pilot tests only -- the point of this feature is keyboard behavior in a
real (headless) app, not a pure function. What is pinned: "/" opens the
dropdown with the registry's entries, typing narrows it with the palette's
own matcher, Tab/Enter complete without submitting, Esc dismisses, and
deleting the leading "/" makes it go away (and re-arms it for the next one).
"""

from __future__ import annotations

import pytest

from doxa import commands
from doxa.app import DoxaApp, SlashComplete, TurnBlock
from tests.fakes import FakeEngine


async def _app(monkeypatch, tmp_path, script=None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    fake = FakeEngine(script or [])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    return DoxaApp(cwd=str(tmp_path)), fake


async def _type(pilot, text: str) -> None:
    for char in text:
        await pilot.press("slash" if char == "/" else char)


@pytest.mark.asyncio
async def test_slash_opens_the_dropdown_with_every_registry_entry(
    monkeypatch, tmp_path
):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        dropdown = app.query_one("#slash-complete", SlashComplete)
        assert dropdown.is_open is False

        await _type(pilot, "/")
        await pilot.pause()
        assert dropdown.is_open is True
        # One registry, two surfaces: the dropdown lists exactly what the
        # registry declares, in the order it declares it.
        assert [c.name for c in dropdown.matches] == commands.names()


@pytest.mark.asyncio
async def test_typing_filters_to_one_command(monkeypatch, tmp_path):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/pe")
        await pilot.pause()
        dropdown = app.query_one("#slash-complete", SlashComplete)
        assert dropdown.is_open is True
        assert [c.name for c in dropdown.matches] == ["/peers"]


@pytest.mark.asyncio
async def test_enter_completes_instead_of_submitting(monkeypatch, tmp_path):
    """The Enter that picks an entry must not also send a turn -- that is
    the whole reason the key protocol lives on the input, ahead of the
    input's own submit binding."""
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/pe")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        prompt = app.query_one("#prompt-input")
        assert prompt.value == "/peers"  # completed, no trailing arg space
        assert app.query_one("#slash-complete", SlashComplete).is_open is False
        assert list(app.query(TurnBlock)) == []  # nothing was sent


@pytest.mark.asyncio
async def test_enter_sends_when_the_command_is_already_typed_in_full(
    monkeypatch, tmp_path
):
    """A fully typed command must cost ONE Enter, not two -- completing
    "/peers" into "/peers" is not a completion, so Enter falls through to
    the input's own submit."""
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/peers")
        await pilot.pause()
        assert app.query_one("#slash-complete", SlashComplete).is_open is True

        await pilot.press("enter")
        for _ in range(100):
            if app.query_one("#prompt-input").value == "":
                break
            await pilot.pause(0.02)
        assert app.query_one("#prompt-input").value == ""  # it was sent
        assert app.query_one("#slash-complete", SlashComplete).is_open is False


@pytest.mark.asyncio
async def test_tab_completes_an_argument_command_with_a_trailing_space(
    monkeypatch, tmp_path
):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/ms")
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        prompt = app.query_one("#prompt-input")
        assert prompt.value == "/msg "  # caret sits where the argument goes
        assert prompt.cursor_position == len("/msg ")
        assert app.query_one("#slash-complete", SlashComplete).is_open is False


@pytest.mark.asyncio
async def test_arrow_keys_move_the_highlight(monkeypatch, tmp_path):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/")
        await pilot.pause()
        dropdown = app.query_one("#slash-complete", SlashComplete)
        assert dropdown.highlighted == 0
        await pilot.press("down")
        await pilot.pause()
        assert dropdown.highlighted == 1
        await pilot.press("up")
        await pilot.press("up")  # wraps to the end
        await pilot.pause()
        assert dropdown.highlighted == len(dropdown.matches) - 1

        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#prompt-input").value.startswith(
            dropdown_last := commands.names()[-1]
        )
        assert dropdown_last  # the highlighted row is what completed


@pytest.mark.asyncio
async def test_escape_dismisses_and_stays_dismissed_for_the_line(
    monkeypatch, tmp_path
):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/p")
        await pilot.pause()
        dropdown = app.query_one("#slash-complete", SlashComplete)
        assert dropdown.is_open is True

        await pilot.press("escape")
        await pilot.pause()
        assert dropdown.is_open is False

        # Typing more of the same "/" line must not resurrect it.
        await _type(pilot, "e")
        await pilot.pause()
        assert dropdown.is_open is False


@pytest.mark.asyncio
async def test_deleting_the_leading_slash_closes_and_rearms(monkeypatch, tmp_path):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/pe")
        await pilot.pause()
        dropdown = app.query_one("#slash-complete", SlashComplete)
        assert dropdown.is_open is True

        for _ in range(3):
            await pilot.press("backspace")
        await pilot.pause()
        assert dropdown.is_open is False
        assert app.query_one("#prompt-input").value == ""

        # Re-armed: the next "/" opens a fresh dropdown.
        await _type(pilot, "/")
        await pilot.pause()
        assert dropdown.is_open is True


@pytest.mark.asyncio
async def test_dropdown_closes_once_arguments_are_being_typed(
    monkeypatch, tmp_path
):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/msg")
        await pilot.pause()
        dropdown = app.query_one("#slash-complete", SlashComplete)
        assert dropdown.is_open is True
        await pilot.press("space")
        await pilot.pause()
        assert dropdown.is_open is False


@pytest.mark.asyncio
async def test_plain_prompt_never_opens_the_dropdown(monkeypatch, tmp_path):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "peers")  # the command's letters, no leading "/"
        await pilot.pause()
        assert app.query_one("#slash-complete", SlashComplete).is_open is False

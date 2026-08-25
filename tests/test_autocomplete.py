# SPDX-License-Identifier: AGPL-3.0-only
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
        # One registry, several surfaces -- and ONE ordering: functional
        # group, then alphabetical inside it. Never insertion order.
        assert [c.name for c in dropdown.matches] == commands.names()
        assert [c.name for c in dropdown.matches] == [
            c.name for group, cmds in commands.grouped() for c in cmds
        ]
        for _group, cmds in commands.grouped():
            names = [c.name for c in cmds]
            assert names == sorted(names)


@pytest.mark.asyncio
async def test_typing_filters_to_one_command(monkeypatch, tmp_path):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/pe")
        await pilot.pause()
        dropdown = app.query_one("#slash-complete", SlashComplete)
        assert dropdown.is_open is True
        # Fuzzy, so "/pe" also reaches /update ("u-P-d-a-t-E") -- but the
        # literal prefix match ranks first, which is the property that
        # matters when someone types the start of a command name.
        assert dropdown.matches[0].name == "/peers"
        assert "/update" in [c.name for c in dropdown.matches]


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
        # Row 0 is a group header; the highlight starts on the first real
        # command below it.
        assert dropdown._rows[0] is None
        assert dropdown.highlighted == 1
        assert dropdown.chosen().name == commands.names()[0]

        await pilot.press("down")
        await pilot.pause()
        assert dropdown.chosen().name == commands.names()[1]

        await pilot.press("up")
        await pilot.press("up")  # wraps past the header to the last command
        await pilot.pause()
        assert dropdown.chosen().name == commands.names()[-1]

        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#prompt-input").value.startswith(
            commands.names()[-1]
        )


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


@pytest.mark.asyncio
async def test_unfiltered_list_shows_dim_group_headers(monkeypatch, tmp_path):
    """Browsing shows the functional groups as header rows -- disabled
    Options, so they are dim and cannot be selected."""
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/")
        await pilot.pause()
        dropdown = app.query_one("#slash-complete", SlashComplete)

        rendered = [
            str(dropdown.get_option_at_index(i).prompt)
            for i in range(dropdown.option_count)
        ]
        expected_groups = [g for g, _cmds in commands.grouped()]
        assert [r for r in rendered if r in expected_groups] == expected_groups

        # Headers are disabled rows, and the row map marks them as gaps.
        header_indexes = [i for i, row in enumerate(dropdown._rows) if row is None]
        assert header_indexes
        for index in header_indexes:
            assert dropdown.get_option_at_index(index).disabled is True

        # An empty group is never given a header.
        assert len(expected_groups) <= len(commands.GROUPS)
        for group in expected_groups:
            assert any(c.group == group for c in commands.REGISTRY)


@pytest.mark.asyncio
async def test_a_header_can_never_be_highlighted_or_completed(
    monkeypatch, tmp_path
):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/")
        await pilot.pause()
        dropdown = app.query_one("#slash-complete", SlashComplete)
        seen = set()
        for _ in range(len(dropdown._rows) * 2):
            assert dropdown._rows[dropdown.highlighted] is not None
            seen.add(dropdown.chosen().name)
            await pilot.press("down")
            await pilot.pause()
        # Arrowing all the way round reaches every command and no header.
        assert seen == set(commands.names())


@pytest.mark.asyncio
async def test_filtering_collapses_headers_and_ranks_by_match(
    monkeypatch, tmp_path
):
    """Someone typing has already said what they want: rank by match
    quality, alphabetically to break ties, and drop the group headers that
    would otherwise push the best match down."""
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/l")
        await pilot.pause()
        dropdown = app.query_one("#slash-complete", SlashComplete)
        assert None not in dropdown._rows          # headers collapsed
        assert dropdown._rows == dropdown.matches
        assert dropdown.highlighted == 0

        from textual.fuzzy import Matcher

        matcher = Matcher("/l")
        expected = sorted(
            (c for c in commands.ordered() if matcher.match(c.name) > 0),
            key=lambda c: (-matcher.match(c.name), c.name),
        )
        assert [c.name for c in dropdown.matches] == [c.name for c in expected]
        # Ties really do fall alphabetically.
        scores = [matcher.match(c.name) for c in dropdown.matches]
        for i in range(len(scores) - 1):
            if scores[i] == scores[i + 1]:
                assert dropdown.matches[i].name < dropdown.matches[i + 1].name


def test_every_registry_row_declares_a_known_group():
    for command in commands.REGISTRY:
        assert command.group in commands.GROUPS, command.name


def test_help_and_palette_use_the_registry_ordering(monkeypatch, tmp_path):
    """One source of order, three surfaces -- never three sort orders."""
    from doxa.app import help_text

    text = help_text()
    # /help renders the groups in registry order...
    positions = [text.index(g) for g, _cmds in commands.grouped()]
    assert positions == sorted(positions)
    # ...and the commands alphabetically within each group.
    for group, cmds in commands.grouped():
        section = text.split(group, 1)[1]
        offsets = [section.index(c.call_form()) for c in cmds]
        assert offsets == sorted(offsets), group


@pytest.mark.asyncio
async def test_palette_entries_follow_the_same_order(monkeypatch, tmp_path):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        names = [entry.label for entry in app.doxa_commands()]
        labelled = [c for c in commands.ordered() if c.palette]
        positions = [names.index(c.palette) for c in labelled]
        assert positions == sorted(positions)

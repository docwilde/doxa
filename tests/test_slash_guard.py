# SPDX-License-Identifier: AGPL-3.0-only
"""Reported: "plugin calls don't return anything, e.g. /lore:pending
--cluster does not work. Nothing happens. No error message."

The actual mechanism, confirmed by reading ``SessionPane.on_prompt_submitted``
(``doxa/session/pane.py``): a "/"-prefixed line that ``commands.lookup()``
does not recognize -- true for every unadopted or misspelled command, and
ALSO true on purpose for ``/compact`` and every adopted plugin row, which
are ``passthrough`` and must reach the CLI verbatim -- fell straight
through to ``_run_turn`` with no further check. The CLI's own local-command
interception (the same mechanism that makes an adopted plugin's namespaced
command work at all, see test_plugin_commands_surface.py) finds nothing
staged for an unstaged name and answers with nothing: no DOXA error, no CLI
text, no visible failure of any kind.

``doxa.commands.unreachable_message`` is the fix: the one new check between
"not a DOXA registry row" and "ship it to the CLI as a turn", answering
``None`` (let it through, unmodified) for anything :func:`doxa.commands.names`
already considers reachable -- which is exactly ``/compact`` and every
currently-adopted plugin command, so passthrough is provably untouched --
and a message otherwise, in one of three shapes covered below.

``/lore:pending`` specifically is not a missing command, it is a REFUSED
one (``doxa.claude_plugins.BLOCKLIST``, v0.74.0): ``lore_core`` already runs
in-process inside DOXA, so the Claude-Code-plugin form of the same project
would be a second, out-of-band carrier into the identical belief store.
That refusal deserves its own message, not the generic "unknown command"
one a typo gets.
"""

from __future__ import annotations

import pytest

from doxa import commands, config
from doxa.app import DoxaApp, SystemBlock, TurnBlock
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    config.invalidate()
    yield
    config.invalidate()


def _system_texts(app) -> list[str]:
    return [b.text for b in app.query(SystemBlock) if b.id != "identity-block"]


async def _app(monkeypatch, tmp_path, fake=None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    fake = fake or FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    return app, fake


async def _submit(app, pilot, line: str) -> str:
    """Submit ``line`` and poll for the PAINTED result: a new SystemBlock
    actually mounted into the tree, not merely a worker having been
    scheduled -- the same polling shape test_cc_commands.py's own ``_run``
    uses, for the same reason (a bare ``pilot.pause()`` races the worker)."""
    before = len(_system_texts(app))
    app.query_one("#prompt-input").value = line
    await pilot.press("enter")
    for _ in range(200):
        texts = _system_texts(app)
        if len(texts) > before:
            return texts[-1]
        await pilot.pause(0.02)
    raise AssertionError(f"{line!r} produced no SystemBlock")


# -- unit level: doxa.commands.unreachable_message / is_reachable ---------


def test_registry_rows_are_reachable_interactive_and_passthrough():
    assert commands.is_reachable("/settings") is True  # interactive
    assert commands.is_reachable("/compact") is True  # passthrough
    assert commands.unreachable_message("/compact") is None
    assert commands.unreachable_message("/settings") is None


def test_adopted_plugin_row_is_reachable(monkeypatch, tmp_path):
    from tests.test_plugin_commands_surface import _install_fake_plugin

    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real-claude"))
    _install_fake_plugin(tmp_path / "real-claude", tmp_path / "cache")

    assert commands.is_reachable("/caveman:caveman") is True
    assert commands.unreachable_message("/caveman:caveman") is None


def test_unknown_command_is_unreachable_with_no_suggestion():
    message = commands.unreachable_message("/xyzzyzzy")
    assert message is not None
    assert message.startswith("unknown command: /xyzzyzzy")
    assert "did you mean" not in message


def test_near_miss_of_a_registry_name_is_suggested():
    message = commands.unreachable_message("/setings")
    assert message is not None
    assert "did you mean /settings?" in message


def test_lore_pending_is_the_blocked_plugin_message_not_generic_unknown():
    message = commands.unreachable_message("/lore:pending")
    assert message is not None
    assert "blocked" in message
    assert "lore" in message
    # the DOXA-native surfaces that already do lore's job -- checked
    # against the live registry, not assumed
    assert "/beliefs" in commands.names()
    assert "/pending" in commands.names()
    assert "/beliefs" in message and "/pending" in message
    # not conflated with a mere typo of some other registry row
    assert "did you mean" not in message


def test_bare_slash_and_slash_with_only_a_space_do_not_crash():
    assert commands.unreachable_message("/") is not None
    assert commands.unreachable_message("/ ") is not None


# -- pane level: the actual reported symptom, end to end -------------------


@pytest.mark.asyncio
async def test_unknown_slash_command_produces_systemblock_and_starts_no_turn(
    monkeypatch, tmp_path,
):
    """THE reported defect, reproduced and closed: before this guard,
    ``/friblesnort`` (or ``/lore:pending``) reached ``_run_turn`` and the
    CLI answered with nothing -- no TurnBlock text, no error, no
    ``fake.received_prompts`` entry either, because a truly silent CLI
    round trip still counts as "the turn worker ran". Asserting
    ``received_prompts == []`` is the honest proof the worker never
    started, not just that its output was empty."""
    app, fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _submit(app, pilot, "/friblesnort")
        assert text.startswith("unknown command: /friblesnort")
        assert fake.received_prompts == []
        assert fake.num_turns == 0
        assert not list(app.query(TurnBlock))


@pytest.mark.asyncio
async def test_lore_pending_with_args_gets_the_specific_blocked_message(
    monkeypatch, tmp_path,
):
    app, fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _submit(app, pilot, "/lore:pending --cluster")
        assert "lore" in text and "blocked" in text
        assert "/beliefs" in text and "/pending" in text
        assert fake.received_prompts == []
        assert not list(app.query(TurnBlock))


@pytest.mark.asyncio
async def test_near_miss_suggestion_reaches_the_pane(monkeypatch, tmp_path):
    """Bare ``"/setings"`` (no trailing space) is close enough for
    ``SlashComplete``'s OWN fuzzy matcher (``textual.fuzzy.Matcher``, a
    different, more lenient algorithm than this guard's ``difflib`` check)
    to open its dropdown with ``/settings`` highlighted -- pre-existing
    UX, not this fix, and arguably a better outcome than the guard ever
    firing (the correction happens before Enter, not after). Trailing
    args (``sync()``: a space closes the dropdown for the rest of the
    line, exactly like a real user who kept typing past the command
    token) is what actually reaches ``on_prompt_submitted`` unintercepted
    -- the same shape the reported ``/lore:pending --cluster`` bug used."""
    app, fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _submit(app, pilot, "/setings --foo")
        assert "did you mean /settings?" in text
        assert fake.received_prompts == []


@pytest.mark.asyncio
async def test_wide_miss_suggests_nothing_absurd(monkeypatch, tmp_path):
    app, fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _submit(app, pilot, "/xyzzyzzy")
        assert text.startswith("unknown command: /xyzzyzzy")
        assert "did you mean" not in text
        assert fake.received_prompts == []


@pytest.mark.asyncio
async def test_compact_still_reaches_the_cli_through_the_guard(
    monkeypatch, tmp_path,
):
    """Constraint 1, at the pane level: the guard sits BEFORE the turn
    dispatch, so if it ever mis-fired on a passthrough row this is where
    it would show up as a regression -- /compact must still become a
    TurnBlock and a received prompt, exactly as test_cc_commands.py's own
    compact test already pins, reasserted here as the guard's own
    negative control."""
    from doxa.engine import EngineEvent

    script = [EngineEvent("turn_started", {}), EngineEvent("turn_done", {})]
    app, fake = await _app(monkeypatch, tmp_path, FakeEngine(script))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "/compact"
        await pilot.press("enter")
        for _ in range(200):
            if list(app.query(TurnBlock)):
                break
            await pilot.pause(0.02)
        blocks = list(app.query(TurnBlock))
        assert len(blocks) == 1
        assert blocks[0].prompt_text == "/compact"
        assert fake.received_prompts == ["/compact"]


@pytest.mark.asyncio
async def test_adopted_plugin_command_still_reaches_the_cli_through_the_guard(
    monkeypatch, tmp_path,
):
    """Same negative control as above, for the OTHER passthrough class:
    an adopted plugin row. test_plugin_commands_surface.py already pins
    this end to end; this reasserts it specifically as proof the guard
    added here does not regress it."""
    from doxa.engine import EngineEvent
    from tests.test_plugin_commands_surface import _install_fake_plugin

    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real-claude"))
    _install_fake_plugin(tmp_path / "real-claude", tmp_path / "cache")

    script = [EngineEvent("turn_started", {}), EngineEvent("turn_done", {})]
    app, fake = await _app(monkeypatch, tmp_path, FakeEngine(script))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "/caveman:caveman ultra"
        await pilot.press("enter")
        for _ in range(200):
            if list(app.query(TurnBlock)):
                break
            await pilot.pause(0.02)
        blocks = list(app.query(TurnBlock))
        assert len(blocks) == 1
        assert blocks[0].prompt_text == "/caveman:caveman ultra"
        assert fake.received_prompts == ["/caveman:caveman ultra"]


@pytest.mark.asyncio
async def test_bare_slash_does_not_crash_the_pane(monkeypatch, tmp_path):
    """A bare ``"/"`` with nothing else typed opens ``SlashComplete``'s own
    grouped-browse dropdown (``PromptInput``'s existing, pre-fix behaviour
    -- ``sync()`` treats ``value == "/"`` as "show everything"), so Enter
    completes the highlighted row instead of submitting -- an existing,
    separate piece of UX this guard does not own or change. A trailing
    space (a bare "/" the user then keeps typing past, or backspaces to
    lose interest in) closes that dropdown (``sync()``: any space means
    "past the command token") and reaches the guard the same way
    "/lore:pending --cluster" does -- that is the shape actually tested
    here, and the guard still must not crash on the resulting name being
    just ``"/"`` once ``on_prompt_submitted`` strips the line."""
    app, fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _submit(app, pilot, "/ ")
        assert text.startswith("unknown command: /")
        assert fake.received_prompts == []


@pytest.mark.asyncio
async def test_lone_slash_with_args_does_not_crash_the_pane(monkeypatch, tmp_path):
    app, fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _submit(app, pilot, "/ foo bar")
        assert text.startswith("unknown command: /")
        assert fake.received_prompts == []

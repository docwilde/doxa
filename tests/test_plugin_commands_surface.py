# SPDX-License-Identifier: AGPL-3.0-only
"""Reported: discovered plugins' commands are not available.

v0.74.0 adopted plugin commands all the way to the underlying ``claude``
CLI (one ``--plugin-dir`` per adopted plugin), and typing the exact
namespaced command genuinely works -- measured directly against a real
adopted plugin for this fix (``caveman:caveman``, isolated CLI, the exact
stream-json path DOXA drives): the bare ``/caveman`` a plugin's own docs
advertise answers ``Unknown command: /caveman``; the namespaced
``/caveman:caveman`` the CLI's own ``system.init`` message lists is the
one that runs.

What was actually missing, ruled in by elimination against the other
three candidate causes (the setting was already checked and reported
plainly; the staged copy already preserves everything a command
references; ``--plugin-dir`` already reaches a spawned session): DOXA's
OWN "/" surfaces -- the prompt's autocomplete dropdown and the Ctrl+P
palette -- read ONLY :data:`doxa.commands.REGISTRY` and never learned a
plugin command existed. docs/plans/plugin-api.md's own premise is that
the palette and autocomplete read ONE registry, so a command cannot exist
on one surface and not the other; adopted plugin commands broke exactly
that, reaching the CLI but neither DOXA surface. :func:`doxa.commands._plugin_rows`
folds them into that one registry, so this file is the "does the fold-in
actually reach both surfaces, end to end" test -- test_claude_plugins.py
covers the invocable-spelling and preview-listing halves directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from doxa import commands, config
from doxa.app import DoxaApp, SlashComplete, TurnBlock
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    config.invalidate()
    yield
    config.invalidate()


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _install_fake_plugin(real_claude: Path, cache: Path) -> None:
    """A single adoptable plugin ('caveman@caveman', one command, no
    hooks/mcp), installed the same shape test_claude_plugins.py's own
    fixtures use -- enough to exercise discover() -> adopt() for real
    rather than mocking either away."""
    plugin_dir = cache / "caveman"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    _write_json(plugin_dir / ".claude-plugin" / "plugin.json", {"name": "caveman"})
    (plugin_dir / "commands").mkdir()
    (plugin_dir / "commands" / "caveman.md").write_text(
        "---\ndescription: Switch caveman intensity level\n"
        'argument-hint: "[lite|full|ultra]"\n---\n'
        "Switch to caveman $ARGUMENTS mode.\n",
        encoding="utf-8",
    )
    _write_json(
        real_claude / "plugins" / "installed_plugins.json",
        {"version": 2, "plugins": {
            "caveman@caveman": [
                {"scope": "user", "installPath": str(plugin_dir), "version": "1.0.0"},
            ],
        }},
    )
    _write_json(
        real_claude / "settings.json",
        {"enabledPlugins": {"caveman@caveman": True}},
    )


async def _app(monkeypatch, tmp_path, fake=None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    fake = fake or FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    return app, fake


async def _type(pilot, text: str) -> None:
    for char in text:
        await pilot.press("slash" if char == "/" else char)


# -- unit: the registry fold-in itself -------------------------------------


def test_no_plugin_rows_with_adoption_off(monkeypatch, tmp_path):
    """Default posture (the setting is off): nothing is adopted, so
    nothing shows up on either surface -- the one candidate cause that
    would NOT have been a bug (offering a command that cannot possibly
    run would be worse than offering none)."""
    monkeypatch.delenv("DOXA_ADOPT_PLUGINS", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real-claude"))
    _install_fake_plugin(tmp_path / "real-claude", tmp_path / "cache")

    names = [c.name for c in commands.ordered()]
    assert "/caveman:caveman" not in names
    assert all(":" not in n for n in names)  # no namespaced plugin row at all


def test_plugin_row_folds_into_ordered_grouped_and_names(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real-claude"))
    _install_fake_plugin(tmp_path / "real-claude", tmp_path / "cache")

    names = commands.names()
    assert "/caveman:caveman" in names

    row = next(c for c in commands.ordered() if c.name == "/caveman:caveman")
    assert row.group == "Plugins"
    assert row.passthrough is True
    assert row.palette_prefill is True
    assert row.summary == "Switch caveman intensity level"

    grouped = dict(commands.grouped())
    assert "Plugins" in grouped
    assert row in grouped["Plugins"]


def test_plugin_row_is_passthrough_not_interactive(monkeypatch, tmp_path):
    """The closure invariant (_command_handlers().keys() ==
    interactive_names()) must survive this fold-in untouched: a plugin
    command has no DOXA-side handler and must never be asked to have one
    -- the underlying CLI is what runs it."""
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real-claude"))
    _install_fake_plugin(tmp_path / "real-claude", tmp_path / "cache")

    assert "/caveman:caveman" not in commands.interactive_names()
    assert commands.find("/caveman:caveman") is None  # REGISTRY-only, unaffected
    assert commands.lookup("/caveman:caveman ultra") is None


# -- end to end: the dropdown, the palette, the wire itself ----------------


@pytest.mark.asyncio
async def test_dropdown_lists_the_adopted_plugin_command(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real-claude"))
    _install_fake_plugin(tmp_path / "real-claude", tmp_path / "cache")

    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        dropdown = app.query_one("#slash-complete", SlashComplete)
        await _type(pilot, "/caveman:caveman")
        await pilot.pause()
        assert dropdown.is_open is True
        assert [c.name for c in dropdown.matches] == ["/caveman:caveman"]
        # v0.28.0 bar: the row actually painted something, not just a
        # query match.
        assert dropdown.region.height > 0


@pytest.mark.asyncio
async def test_dropdown_completion_prefills_without_running_it(monkeypatch, tmp_path):
    """palette_prefill's whole point for a plugin row: DOXA has no
    handler for it, so completing it must land the text in the prompt for
    the user to submit -- never try to run it as a DOXA command."""
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real-claude"))
    _install_fake_plugin(tmp_path / "real-claude", tmp_path / "cache")

    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/caveman:caveman")
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        prompt = app.query_one("#prompt-input")
        assert prompt.value == "/caveman:caveman "  # usage present -> trailing space
        assert not list(app.query(TurnBlock))  # nothing submitted yet


@pytest.mark.asyncio
async def test_submitted_plugin_command_reaches_the_engine_untouched(
    monkeypatch, tmp_path,
):
    """The mechanism this whole fix depends on, driven end to end: typing
    the plugin's exact invocable name and pressing Enter must NOT be
    intercepted by DOXA (there is no handler for it) -- it has to reach
    the engine as the literal prompt text, the same passthrough path
    /compact already rides, so the underlying CLI (which DOES know this
    command once adopted) is the one that expands it."""
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real-claude"))
    _install_fake_plugin(tmp_path / "real-claude", tmp_path / "cache")

    app, fake = await _app(monkeypatch, tmp_path)
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
async def test_palette_entry_prefills_the_plugin_command(monkeypatch, tmp_path):
    """The palette's OTHER path (_cmd_run_slash) calls a pane handler
    these rows do not have -- a plugin row MUST use the prefill callback,
    never the run-slash one, or selecting it from Ctrl+P would print
    "unknown command" instead of doing anything."""
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real-claude"))
    _install_fake_plugin(tmp_path / "real-claude", tmp_path / "cache")

    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        entries = app.doxa_commands()
        entry = next(e for e in entries if e.label == "Plugin: /caveman:caveman")
        entry.callback()
        await pilot.pause()
        prompt = app.query_one("#prompt-input")
        assert prompt.value == "/caveman:caveman "
        assert not list(app.query(TurnBlock))

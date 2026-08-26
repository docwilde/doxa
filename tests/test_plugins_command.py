# SPDX-License-Identifier: AGPL-3.0-only
"""docs/plans/plugins.md -- ``/plugins`` and ``/reload-plugins``.

Same bar as every other user-visible surface since v0.28.0: assert
RENDERED text and non-zero height, not that a query merely matched
something. ``/plugins`` and ``/reload-plugins`` are plain read-only
SystemBlocks (like ``/doctor`` and ``/context``), so the geometry
assertion here is the same shape ``tests/test_errors.py`` uses for its
blocks: ``region.height > 0``.
"""

from __future__ import annotations

import json

import pytest

from doxa import commands as commands_mod
from doxa.app import DoxaApp, SystemBlock
from doxa.ui.labels import help_text
from tests.fakes import FakeEngine


async def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    fake = FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    return DoxaApp(cwd=str(tmp_path)), fake


async def _run(app, pilot, line: str) -> "SystemBlock":
    app.query_one("#prompt-input").value = line
    before = len([b for b in app.query(SystemBlock) if b.id != "identity-block"])
    await pilot.press("enter")
    for _ in range(300):
        blocks = [b for b in app.query(SystemBlock) if b.id != "identity-block"]
        if len(blocks) > before:
            # The block exists in the DOM the instant it mounts, but its
            # region is not laid out until a later refresh -- a couple
            # more pauses let that happen before a caller reads geometry
            # off it (same race test_errors.py's _settle sidesteps).
            for _ in range(10):
                await pilot.pause(0.02)
            return blocks[-1]
        await pilot.pause(0.02)
    raise AssertionError(f"{line!r} produced no output block")


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _install_fake_plugin(base, tmp_path, scope_key="caveman@caveman", enabled=True):
    plugin_dir = tmp_path / "cache" / "caveman"
    (plugin_dir / "commands").mkdir(parents=True)
    (plugin_dir / "commands" / "cmd.md").write_text("do a thing")
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    _write_json(plugin_dir / ".claude-plugin" / "plugin.json", {"description": "test plugin"})
    _write_json(base / "plugins" / "installed_plugins.json", {
        "version": 2,
        "plugins": {scope_key: [{"scope": "user", "installPath": str(plugin_dir), "version": "1.0.0"}]},
    })
    _write_json(base / "settings.json", {"enabledPlugins": {scope_key: enabled}})
    return plugin_dir


# -- registry closure ------------------------------------------------------


def test_plugins_and_reload_plugins_are_registered_everywhere():
    assert "/plugins" in commands_mod.interactive_names()
    assert "/reload-plugins" in commands_mod.interactive_names()
    assert commands_mod.find("/plugins") is not None
    assert commands_mod.find("/reload-plugins") is not None
    assert "/plugins" in help_text()
    assert "/reload-plugins" in help_text()


@pytest.mark.asyncio
async def test_plugins_reaches_the_palette_and_autocomplete(monkeypatch, tmp_path):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        labels = {e.label for e in app.doxa_commands()}
        assert "Plugins: list" in labels
        assert "Plugins: reload" in labels


# -- /plugins: what the user sees ------------------------------------------


@pytest.mark.asyncio
async def test_plugins_reports_off_by_default_with_nothing_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real-claude"))
    (tmp_path / "real-claude").mkdir()
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _run(app, pilot, "/plugins")
        assert block.region.height > 0, "plugins report mounted at zero rows"
        assert "adoption: OFF" in block.text
        assert "no Claude Code plugins installed" in block.text


@pytest.mark.asyncio
async def test_plugins_lists_a_discovered_plugin_and_marks_it_would_adopt(monkeypatch, tmp_path):
    base = tmp_path / "real-claude"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    _install_fake_plugin(base, tmp_path)
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _run(app, pilot, "/plugins")
        assert block.region.height > 0
        assert "caveman@caveman" in block.text
        assert "adoption: OFF" in block.text
        # Discovered and adoptable, but not adopted while the setting is off.
        assert "○ caveman@caveman" in block.text


@pytest.mark.asyncio
async def test_plugins_marks_an_adopted_plugin_when_the_setting_is_on(monkeypatch, tmp_path):
    base = tmp_path / "real-claude"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    _install_fake_plugin(base, tmp_path)
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _run(app, pilot, "/plugins")
    assert "✓ caveman@caveman" in block.text
    assert "1 plugin(s) discovered, 1 adopted" in block.text


@pytest.mark.asyncio
async def test_plugins_names_lore_and_says_why_it_is_refused(monkeypatch, tmp_path):
    base = tmp_path / "real-claude"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    plugin_dir = tmp_path / "cache" / "lore"
    (plugin_dir / "commands").mkdir(parents=True)
    (plugin_dir / "commands" / "ask.md").write_text("do lore things")
    _write_json(base / "plugins" / "installed_plugins.json", {
        "version": 2,
        "plugins": {"lore@lore": [{"scope": "user", "installPath": str(plugin_dir), "version": "0.34.0"}]},
    })
    _write_json(base / "settings.json", {"enabledPlugins": {"lore@lore": True}})
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _run(app, pilot, "/plugins")
    assert "✗ lore@lore" in block.text
    assert "lore_core" in block.text
    assert "0 adopted" in block.text


# -- /reload-plugins: what it can and cannot do -----------------------------


@pytest.mark.asyncio
async def test_reload_plugins_states_it_only_affects_new_sessions(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real-claude"))
    (tmp_path / "real-claude").mkdir()
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _run(app, pilot, "/reload-plugins")
        assert block.region.height > 0
        assert "NEW sessions and tabs only" in block.text
        assert "cannot hand a running claude process a new one" in block.text or "connected with" in block.text


@pytest.mark.asyncio
async def test_reload_plugins_re_stages_after_the_setting_is_turned_on(monkeypatch, tmp_path):
    """Re-running /reload-plugins after DOXA_ADOPT_PLUGINS flips on must
    actually re-stage: the report right after reflects the fresh scan, not
    a cached one from before the setting changed."""
    base = tmp_path / "real-claude"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    _install_fake_plugin(base, tmp_path)
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        off_block = await _run(app, pilot, "/reload-plugins")
        assert "0 adopted" in off_block.text

        monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
        on_block = await _run(app, pilot, "/reload-plugins")
        assert "1 adopted" in on_block.text
        assert "re-staged 1 plugin(s)" in on_block.text

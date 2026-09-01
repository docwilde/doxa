# SPDX-License-Identifier: AGPL-3.0-only
"""Settings: the precedence rule, the file, and the modal that writes it.

The rule under test is one line -- environment > config file > default --
and it is tested where it actually bites: through the SAME lookup the
engine, the app and the image ladder now use, not through a private helper.
The modal is tested for what a settings menu can get wrong: rows that
don't correspond to real knobs, a change that doesn't survive, and an edit
that silently loses to an env var without saying so.
"""

from __future__ import annotations

import pytest

from doxa import config, engine, images
from doxa.app import DoxaApp, git_branch_symbol
from textual.widgets import Input, TabbedContent

from doxa.settings import CATEGORIES, SettingsScreen, field_id
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    """Every test in this module gets its own DOXA_HOME -- nothing here
    may read or write the developer's real ~/.doxa."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    config.invalidate()
    yield
    config.invalidate()


# -- the precedence rule --------------------------------------------------


def test_default_when_neither_env_nor_file_says_anything(monkeypatch):
    monkeypatch.delenv("DOXA_DERIVE_SECS", raising=False)
    assert config.raw("DOXA_DERIVE_SECS") == ""
    # The registry's `default` and the READER's own default are two
    # separate statements; this test keeps them equal. v0.98.0 set
    # both to 900 -- `config.raw` still returns "" because nothing
    # is written anywhere, and the reader supplies the value.
    assert engine.derive_interval() == engine.DERIVE_SECS_DEFAULT == 900.0


def test_config_file_is_read_when_the_env_is_silent(monkeypatch):
    monkeypatch.delenv("DOXA_DERIVE_SECS", raising=False)
    config.save({"derive_secs": "45"})
    assert config.raw("DOXA_DERIVE_SECS") == "45"
    assert engine.derive_interval() == 45.0


def test_env_beats_the_config_file(monkeypatch):
    """The whole precedence rule in one assertion: a stored value loses to
    an env var, because the env var is the narrower, more deliberate act."""
    config.save({"derive_secs": "45"})
    monkeypatch.setenv("DOXA_DERIVE_SECS", "5")
    assert config.raw("DOXA_DERIVE_SECS") == "5"
    assert engine.derive_interval() == 5.0
    assert config.overridden_by_env("DOXA_DERIVE_SECS") is True


def test_every_knob_in_the_menu_reaches_its_real_reader(monkeypatch):
    """No placeholder rows: each writable setting is proved to change the
    behavior of the code its help line names."""
    monkeypatch.delenv("DOXA_DERIVE_SECS", raising=False)
    monkeypatch.delenv("DOXA_CONSULT_FLOOR", raising=False)
    monkeypatch.delenv("DOXA_NERD_FONT", raising=False)
    monkeypatch.delenv("DOXA_IMAGE_MODE", raising=False)
    monkeypatch.delenv("DOXA_MODEL", raising=False)
    monkeypatch.delenv("DOXA_LINGER_SECS", raising=False)
    monkeypatch.delenv("DOXA_BACKGROUND", raising=False)

    config.save({
        "derive_secs": "30",
        "consult_floor": "2.5",
        "nerd_font": "1",
        "image_mode": "sixel",
        "model": "claude-haiku-4-5",
        "linger_secs": "9",
        "background": "transparent",
    })
    assert engine.derive_interval() == 30.0
    assert engine.consult_floor() == 2.5
    assert git_branch_symbol() == ""
    assert images.detect_mode() == "sixel"
    assert config.model() == "claude-haiku-4-5"
    assert config.linger_secs() == 9.0
    assert config.background_mode() == "transparent"


def test_emptying_a_field_returns_the_knob_to_its_default(monkeypatch):
    monkeypatch.delenv("DOXA_CONSULT_FLOOR", raising=False)
    config.save({"consult_floor": "2.5"})
    assert engine.consult_floor() == 2.5
    config.save({"consult_floor": ""})
    assert "consult_floor" not in config.load()
    assert engine.consult_floor() == engine.DEFAULT_CONSULT_FLOOR


def test_a_broken_config_file_costs_settings_not_the_session():
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not = [ toml", encoding="utf-8")
    config.invalidate()
    assert config.load() == {}
    assert config.raw("DOXA_DERIVE_SECS") == ""


def test_unknown_keys_survive_a_save():
    """A config written by a newer DOXA must not be gutted by an older one."""
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('future_knob = "keep me"\n', encoding="utf-8")
    config.invalidate()
    config.save({"derive_secs": "12"})
    assert config.load()["future_knob"] == "keep me"
    assert config.load()["derive_secs"] == 12


def test_saved_file_is_user_only():
    config.save({"derive_secs": "12"})
    assert (config.config_path().stat().st_mode & 0o777) == 0o600


# -- the modal ------------------------------------------------------------


async def _open_settings(app, pilot) -> SettingsScreen:
    for _ in range(200):
        if isinstance(app.screen, SettingsScreen):
            return app.screen
        await pilot.pause(0.02)
    raise AssertionError("settings modal never opened")


@pytest.mark.asyncio
async def test_modal_mounts_a_row_per_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        screen = await _open_settings(app, pilot)
        # Every row is present, on its category tab...
        labels = {str(n.renderable) for n in screen.query(".setting-label")}
        for setting in config.SETTINGS:
            assert setting.label in labels
        # ...and every row that is NOT env-shadowed offers a field.
        for setting in screen.editable():
            assert screen.query(f"#{field_id(setting.key)}")
        # ...while display-only rows offer none.
        assert not screen.query("#setting-")


@pytest.mark.asyncio
async def test_every_category_renders_its_own_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        screen = await _open_settings(app, pilot)
        for category in CATEGORIES:
            pane = screen.query_one(f"#settings-cat-{category.lower()}")
            labels = {str(n.renderable) for n in pane.query(".setting-label")}
            expected = {
                s.label for s in config.SETTINGS if s.category == category
            }
            assert expected <= labels, category
            assert labels, f"{category} tab is empty"


@pytest.mark.asyncio
async def test_category_keys_do_not_move_the_app_tabs(monkeypatch, tmp_path):
    """A modal must never move the window's tabs underneath itself."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app_tab_before = app.query_one("#session-tabs", TabbedContent).active
        await pilot.press("ctrl+comma")
        screen = await _open_settings(app, pilot)
        tabs = screen.query_one("#settings-categories", TabbedContent)
        assert tabs.active == "settings-cat-session"
        await pilot.press("shift+right")
        await pilot.pause()
        assert tabs.active == "settings-cat-memory"
        await pilot.press("shift+left")
        await pilot.press("shift+left")  # wraps
        await pilot.pause()
        assert tabs.active == "settings-cat-about"
        assert app.query_one("#session-tabs", TabbedContent).active == app_tab_before


@pytest.mark.asyncio
async def test_unsaved_edits_survive_a_category_switch(monkeypatch, tmp_path):
    """Documented choice: switching category PRESERVES unsaved edits (every
    pane stays mounted). They are written on save, discarded on Esc."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delenv("DOXA_DERIVE_SECS", raising=False)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        screen = await _open_settings(app, pilot)
        screen.query_one(f"#{field_id('derive_secs')}").value = "31"
        await pilot.press("shift+right")
        await pilot.press("shift+left")
        await pilot.pause()
        assert screen.query_one(f"#{field_id('derive_secs')}").value == "31"


@pytest.mark.asyncio
async def test_rows_show_the_effective_value_and_its_source(monkeypatch, tmp_path):
    """The heart of the correction: what a row displays is what is IN
    FORCE, with the provenance that explains it."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delenv("DOXA_DERIVE_SECS", raising=False)
    monkeypatch.delenv("DOXA_LINGER_SECS", raising=False)
    monkeypatch.setenv("DOXA_NERD_FONT", "1")
    config.save({"derive_secs": "900"})
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        screen = await _open_settings(app, pilot)
        values = " | ".join(
            str(n.renderable) for n in screen.query(".setting-value")
        )
        assert "900   (config)" in values          # from the file
        assert "120   (default)" in values         # linger, untouched
        assert "1   (env DOXA_NERD_FONT — overrides config)" in values


@pytest.mark.asyncio
async def test_an_env_shadowed_row_is_read_only_and_says_why(monkeypatch, tmp_path):
    """No silent no-ops: a row the environment is winning offers no field
    at all, and explains itself."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("DOXA_DERIVE_SECS", "5")
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        screen = await _open_settings(app, pilot)
        assert not screen.query(f"#{field_id('derive_secs')}")
        assert "derive_secs" not in screen.values()
        shadowed = " ".join(
            str(n.renderable) for n in screen.query(".setting-shadowed")
        )
        assert "set by env" in shadowed
        assert "unset DOXA_DERIVE_SECS" in shadowed


@pytest.mark.asyncio
async def test_saving_rereads_and_shows_the_new_effective_value(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delenv("DOXA_DERIVE_SECS", raising=False)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        screen = await _open_settings(app, pilot)
        screen.query_one(f"#{field_id('derive_secs')}").value = "77"
        screen.action_save()
        await pilot.pause()
        values = " | ".join(
            str(n.renderable) for n in screen.query(".setting-value")
        )
        assert "77   (config)" in values
    config.invalidate()
    assert config.load()["derive_secs"] == 77
    assert engine.derive_interval() == 77.0


@pytest.mark.asyncio
async def test_model_row_follows_the_session_not_the_config_default(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delenv("DOXA_MODEL", raising=False)
    config.save({"model": "sonnet"})
    fake = FakeEngine([], model="claude-sonnet-4-5")
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        # A mid-session switch moves the row with it.
        await fake.set_model("haiku")
        await pilot.press("ctrl+comma")
        screen = await _open_settings(app, pilot)
        values = " | ".join(
            str(n.renderable) for n in screen.query(".setting-value")
        )
        assert "haiku   (session — config default is sonnet)" in values


@pytest.mark.asyncio
async def test_paths_tab_shows_real_resolved_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        screen = await _open_settings(app, pilot)
        pane = screen.query_one("#settings-cat-paths")
        values = " | ".join(str(n.renderable) for n in pane.query(".setting-value"))
        assert str(config.doxa_home()) in values
        assert str(tmp_path / "rt") in values          # the runtime dir, resolved
        assert str(config.config_path()) in values
        # ...and none of them offers an edit.
        assert not pane.query(Input)


@pytest.mark.asyncio
async def test_memory_tab_shows_the_shared_lore_store_read_only(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        screen = await _open_settings(app, pilot)
        pane = screen.query_one("#settings-cat-memory")
        labels = {str(n.renderable) for n in pane.query(".setting-label")}
        assert "lore store" in labels
        notes = " ".join(str(n.renderable) for n in pane.query(".setting-note"))
        assert "Shared with the Claude Code LORE plugin" in notes
        assert "LORE_ROOT" in notes
        import lore_core

        values = " | ".join(str(n.renderable) for n in pane.query(".setting-value"))
        assert str(lore_core.ROOT) in values


@pytest.mark.asyncio
async def test_slash_settings_opens_the_same_modal(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "/settings"
        await pilot.press("enter")
        await _open_settings(app, pilot)


@pytest.mark.asyncio
async def test_escape_cancels_without_writing(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delenv("DOXA_DERIVE_SECS", raising=False)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        screen = await _open_settings(app, pilot)
        screen.query_one(f"#{field_id('derive_secs')}").value = "999"
        await pilot.press("escape")
        for _ in range(200):
            if not isinstance(app.screen, SettingsScreen):
                break
            await pilot.pause(0.02)
    config.invalidate()
    assert config.config_path().exists() is False


# -- the state home -------------------------------------------------------


def test_doxa_home_defaults_and_is_overridable(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "elsewhere"))
    assert config.doxa_home() == tmp_path / "elsewhere"
    assert config.config_path() == tmp_path / "elsewhere" / "config.toml"
    monkeypatch.delenv("DOXA_HOME", raising=False)
    from pathlib import Path

    assert config.doxa_home() == Path.home() / ".doxa"


def test_state_home_is_created_private_on_first_write():
    config.save({"derive_secs": "1"})
    home = config.doxa_home()
    assert home.is_dir()
    assert (home.stat().st_mode & 0o777) == 0o700


def test_legacy_xdg_config_is_migrated_once(monkeypatch, tmp_path):
    """Nobody loses the settings an earlier build wrote under XDG."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    legacy = config.legacy_config_path()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text('derive_secs = 42\n', encoding="utf-8")
    config._MIGRATED = False
    moved = config.migrate_legacy()
    assert moved == config.config_path()
    assert legacy.exists() is False
    assert config.load()["derive_secs"] == 42
    # ...and only once: a second call is a no-op.
    assert config.migrate_legacy() is None


def test_migration_never_overwrites_an_existing_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config.save({"derive_secs": "7"})
    legacy = config.legacy_config_path()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text('derive_secs = 42\n', encoding="utf-8")
    config._MIGRATED = False
    assert config.migrate_legacy() is None
    assert config.load()["derive_secs"] == 7


def test_sockets_stay_in_the_runtime_dir_not_the_state_home(monkeypatch, tmp_path):
    """Durable state and ephemeral endpoints are deliberately split: home
    directories can be NFS (AF_UNIX misbehaves) and stale sockets must not
    outlive a reboot."""
    from doxa import peers

    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "state"))
    assert peers.runtime_dir() == tmp_path / "rt"
    assert config.doxa_home() not in peers.runtime_dir().parents
    assert peers.registry_dir().is_relative_to(peers.runtime_dir())

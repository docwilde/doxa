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
from doxa.settings import SettingsScreen, field_id
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    """Every test in this module gets its own XDG config home -- nothing
    here may read or write the developer's real ~/.config/doxa."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config.invalidate()
    yield
    config.invalidate()


# -- the precedence rule --------------------------------------------------


def test_default_when_neither_env_nor_file_says_anything(monkeypatch):
    monkeypatch.delenv("DOXA_DERIVE_SECS", raising=False)
    assert config.raw("DOXA_DERIVE_SECS") == ""
    assert engine.derive_interval() is None  # the reader's own default: off


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

    config.save({
        "derive_secs": "30",
        "consult_floor": "2.5",
        "nerd_font": "1",
        "image_mode": "sixel",
        "model": "claude-haiku-4-5",
        "linger_secs": "9",
    })
    assert engine.derive_interval() == 30.0
    assert engine.consult_floor() == 2.5
    assert git_branch_symbol() == ""
    assert images.detect_mode() == "sixel"
    assert config.model() == "claude-haiku-4-5"
    assert config.linger_secs() == 9.0


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
        for setting in config.SETTINGS:
            if setting.key and not setting.read_only:
                assert screen.query(f"#{field_id(setting.key)}")
        # The read-only row (LORE's store path) is shown, and is NOT a field.
        assert len(screen.query(".setting-readonly")) == 1
        assert len(screen.query(".setting-label")) == len(config.SETTINGS)
        assert "settings" in str(screen.query_one("#settings-title").renderable)


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
async def test_a_change_persists_and_is_reread_on_next_construction(
    monkeypatch, tmp_path
):
    """The acceptance test for a settings menu: type it, save it, and a
    freshly constructed reader sees it."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delenv("DOXA_DERIVE_SECS", raising=False)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        screen = await _open_settings(app, pilot)
        field = screen.query_one(f"#{field_id('derive_secs')}")
        field.value = "77"
        screen.action_save()
        for _ in range(200):
            if not isinstance(app.screen, SettingsScreen):
                break
            await pilot.pause(0.02)

    # A brand-new process would read the file; a brand-new cache read is the
    # same proof without spawning one.
    config.invalidate()
    assert config.load()["derive_secs"] == 77
    assert engine.derive_interval() == 77.0


@pytest.mark.asyncio
async def test_escape_cancels_without_writing(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        screen = await _open_settings(app, pilot)
        screen.query_one("#setting-derive_secs").value = "999"
        await pilot.press("escape")
        for _ in range(200):
            if not isinstance(app.screen, SettingsScreen):
                break
            await pilot.pause(0.02)
    config.invalidate()
    assert config.config_path().exists() is False


@pytest.mark.asyncio
async def test_modal_says_so_when_the_environment_is_winning(
    monkeypatch, tmp_path
):
    """An edit that cannot take effect must SAY it cannot take effect."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("DOXA_DERIVE_SECS", "5")
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        screen = await _open_settings(app, pilot)
        text = " ".join(
            str(node.renderable) for node in screen.query(".setting-label")
        )
        assert "[env]" in text
        helps = " ".join(
            str(node.renderable) for node in screen.query(".setting-help")
        )
        assert "DOXA_DERIVE_SECS is set in the environment and wins" in helps

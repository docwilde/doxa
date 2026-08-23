"""/setup -- check state, fix findings ONE at a time.

The finding logic (doxa.setup's checks) is pure data-producing functions,
tested directly without a running TUI, same discipline as
doxa.commands' registry. SetupScreen itself gets a handful of pilot tests
for the wizard's walking behaviour: apply a choice, skip a step, an
info-only step just advances, and the model/effort step's hand-off to the
settings modal.

DOXA_SKIP_FIRST_RUN is cleared (conftest.py sets it suite-wide) wherever a
test actually exercises the auto-first-launch trigger; every other test
here runs with it still set, so opening a DoxaApp never race-triggers the
wizard underneath the thing being tested.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from doxa import auth as auth_mod
from doxa import config as config_mod
from doxa import setup as setup_mod
from doxa import _lore_bootstrap
from doxa.app import DoxaApp
from doxa.settings import SettingsScreen
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


# -- the marker / first-run gate ------------------------------------------


def test_needs_first_run_true_until_marked(monkeypatch):
    monkeypatch.delenv("DOXA_SKIP_FIRST_RUN", raising=False)
    assert setup_mod.needs_first_run() is True
    setup_mod.mark_seen()
    assert setup_mod.needs_first_run() is False


def test_the_test_suite_kill_switch_wins_even_with_no_marker(monkeypatch):
    monkeypatch.setenv("DOXA_SKIP_FIRST_RUN", "1")
    assert not setup_mod.marker_path().exists()
    assert setup_mod.needs_first_run() is False


# -- individual findings ---------------------------------------------------


def test_auth_finding_is_info_only_and_never_offers_a_fix(monkeypatch):
    monkeypatch.setattr(
        auth_mod,
        "PROVIDERS",
        {"claude": auth_mod.PROVIDERS["claude"]},
    )
    monkeypatch.setattr(auth_mod.AuthProvider, "installed", lambda self: True)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0),
    )
    finding = setup_mod._auth_finding()
    assert finding.info_only is True
    assert finding.choices == ()
    assert "authenticated" in finding.state


def test_auth_finding_reports_not_authenticated(monkeypatch):
    monkeypatch.setattr(auth_mod.AuthProvider, "installed", lambda self: True)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1),
    )
    finding = setup_mod._auth_finding()
    assert "not authenticated" in finding.state


def test_lore_store_finding_env_wins_and_is_info_only(monkeypatch):
    monkeypatch.setenv("LORE_ROOT", "/tmp/somewhere-env-said-so")
    finding = setup_mod._lore_store_finding()
    assert finding.info_only is True
    assert "/tmp/somewhere-env-said-so" in finding.state


def test_lore_store_finding_remembers_a_previous_choice(monkeypatch, tmp_path):
    monkeypatch.delenv("LORE_ROOT", raising=False)
    config_mod.save_lore_root(str(tmp_path / "sticky-store"))
    finding = setup_mod._lore_store_finding()
    assert finding.info_only is True
    assert "sticky-store" in finding.state


def test_lore_store_finding_asks_when_a_plugin_store_is_detected(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("LORE_ROOT", raising=False)
    plugin_dir = tmp_path / "plugin-lore"
    plugin_dir.mkdir()
    monkeypatch.setattr(setup_mod, "PLUGIN_LORE_DIR", plugin_dir)
    finding = setup_mod._lore_store_finding()
    assert finding.info_only is False
    assert len(finding.choices) == 2
    assert "plugin" in finding.choices[0].label

    # Applying the first choice stickies the plugin's store.
    message = finding.choices[0].apply()
    assert str(plugin_dir) in message
    assert config_mod.load()["lore_root"] == str(plugin_dir)
    source, value = config_mod.provenance("LORE_ROOT")
    assert source == "config"
    assert value == str(plugin_dir)


def test_lore_store_finding_creates_a_doxa_store_when_nothing_exists(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("LORE_ROOT", raising=False)
    monkeypatch.setattr(setup_mod, "PLUGIN_LORE_DIR", tmp_path / "no-such-plugin-dir")
    finding = setup_mod._lore_store_finding()
    assert finding.info_only is False
    assert len(finding.choices) == 1
    message = finding.choices[0].apply()
    doxa_store = config_mod.doxa_home() / "lore"
    assert doxa_store.is_dir()
    assert str(doxa_store) in message
    assert config_mod.load()["lore_root"] == str(doxa_store)


def test_migrate_finding_skips_cleanly_when_absent():
    finding = setup_mod._migrate_finding()
    assert finding.choices == ()
    assert "skipped" in finding.skip_note


def test_migrate_finding_offers_to_run_it_when_present(monkeypatch):
    from doxa import commands as commands_mod

    fake_cmd = commands_mod.SlashCommand(
        name="/migrate", group="Maintenance", summary="test-only",
    )
    monkeypatch.setattr(commands_mod, "find", lambda name: fake_cmd if name == "/migrate" else None)
    finding = setup_mod._migrate_finding()
    assert finding.skip_note == ""
    assert len(finding.choices) == 1


def test_model_effort_finding_hands_off_to_settings():
    finding = setup_mod._model_effort_finding()
    assert finding.action == setup_mod.ACTION_OPEN_SETTINGS
    assert len(finding.choices) == 1
    assert "CLI default" in finding.state


def test_collect_findings_returns_one_per_check(monkeypatch):
    monkeypatch.setenv("LORE_ROOT", "/tmp/x")
    findings = setup_mod.collect_findings()
    assert [f.id for f in findings] == ["auth", "lore-store", "migrate", "model-effort"]


def test_doctor_placeholder_and_summary_text():
    text = setup_mod.summary(["auth: informational", "lore-store: skipped"])
    assert "setup: done." in text
    assert "auth: informational" in text
    assert setup_mod.doctor_placeholder() in text


# -- config.save_lore_root --------------------------------------------------


def test_save_lore_root_bypasses_the_modal_readonly_gate(tmp_path):
    """config.save() -- the settings-modal writer -- must NOT be able to
    write this row (it is read-only there on purpose); save_lore_root is
    the separate, deliberate writer /setup uses."""
    path = str(tmp_path / "chosen-store")
    ignored = config_mod.save({"lore_root": path})
    assert "lore_root" not in config_mod.load()
    config_mod.save_lore_root(path)
    assert config_mod.load()["lore_root"] == path


def test_save_lore_root_preserves_other_settings(tmp_path):
    config_mod.save({"model": "claude-opus-4-5"})
    config_mod.save_lore_root(str(tmp_path / "store"))
    stored = config_mod.load()
    assert stored["model"] == "claude-opus-4-5"
    assert stored["lore_root"] == str(tmp_path / "store")


# -- _lore_bootstrap.export_sticky_lore_root --------------------------------


def test_export_sticky_lore_root_exports_a_stored_choice(monkeypatch, tmp_path):
    monkeypatch.delenv("LORE_ROOT", raising=False)
    config_mod.save_lore_root(str(tmp_path / "sticky"))
    _lore_bootstrap.export_sticky_lore_root()
    assert os.environ["LORE_ROOT"] == str(tmp_path / "sticky")
    del os.environ["LORE_ROOT"]


def test_export_sticky_lore_root_never_overrides_an_existing_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LORE_ROOT", "/already/set/by/the/environment")
    config_mod.save_lore_root(str(tmp_path / "sticky"))
    _lore_bootstrap.export_sticky_lore_root()
    assert os.environ["LORE_ROOT"] == "/already/set/by/the/environment"


def test_export_sticky_lore_root_is_a_noop_with_nothing_stored(monkeypatch):
    monkeypatch.delenv("LORE_ROOT", raising=False)
    _lore_bootstrap.export_sticky_lore_root()
    assert "LORE_ROOT" not in os.environ


# -- the wizard screen, driven end to end -----------------------------------


async def _open_setup(app, pilot):
    for _ in range(200):
        if isinstance(app.screen, setup_mod.SetupScreen) and app.screen.findings:
            return app.screen
        await pilot.pause(0.02)
    raise AssertionError("setup wizard never opened / never finished loading")


@pytest.mark.asyncio
async def test_slash_setup_opens_the_wizard(monkeypatch):
    monkeypatch.setattr(auth_mod.AuthProvider, "installed", lambda self: False)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "/setup"
        await pilot.press("enter")
        screen = await _open_setup(app, pilot)
        assert len(screen.findings) == 4


@pytest.mark.asyncio
async def test_walking_an_info_only_step_with_enter(monkeypatch):
    monkeypatch.setattr(auth_mod.AuthProvider, "installed", lambda self: False)
    monkeypatch.setenv("LORE_ROOT", "/tmp/env-store")  # makes lore-store info-only too
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_setup()
        screen = await _open_setup(app, pilot)
        assert screen.findings[0].id == "auth"
        await pilot.press("enter")
        await pilot.pause()
        assert screen.index == 1
        assert screen.findings[1].id == "lore-store"


@pytest.mark.asyncio
async def test_applying_a_choice_and_skipping_reach_the_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_mod.AuthProvider, "installed", lambda self: False)
    monkeypatch.delenv("LORE_ROOT", raising=False)
    monkeypatch.setattr(setup_mod, "PLUGIN_LORE_DIR", tmp_path / "no-plugin-here")
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_setup()
        screen = await _open_setup(app, pilot)
        await pilot.press("enter")  # auth: info-only, continue
        await pilot.pause()
        assert screen.findings[screen.index].id == "lore-store"
        await pilot.press("1")  # create the doxa store
        await pilot.pause()
        assert screen.findings[screen.index].id == "migrate"
        await pilot.press("enter")  # no /migrate in this build -- auto-skip note
        await pilot.pause()
        assert screen.findings[screen.index].id == "model-effort"
        await pilot.press("s")  # skip -- do not chain into Settings
        await pilot.pause()
        assert screen.index == len(screen.findings)
        body = str(screen.query_one("#setup-body").renderable)
        assert "setup: done." in body
        assert setup_mod.doctor_placeholder() in body
        assert "LORE store:" in body
        assert "/migrate: skipped" in body


@pytest.mark.asyncio
async def test_model_effort_choice_chains_into_the_settings_modal(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_mod.AuthProvider, "installed", lambda self: False)
    monkeypatch.setenv("LORE_ROOT", "/tmp/env-store")
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_setup()
        screen = await _open_setup(app, pilot)
        await pilot.press("enter")  # auth
        await pilot.pause()
        await pilot.press("enter")  # lore-store (env, info-only)
        await pilot.pause()
        assert screen.findings[screen.index].id == "migrate"
        await pilot.press("enter")  # no /migrate in this build -- auto-skip note
        await pilot.pause()
        assert screen.findings[screen.index].id == "model-effort"
        await pilot.press("1")  # open Settings now
        await pilot.pause()
        await pilot.press("escape")  # close the summary
        for _ in range(200):
            if isinstance(app.screen, SettingsScreen):
                break
            await pilot.pause(0.02)
        assert isinstance(app.screen, SettingsScreen)


@pytest.mark.asyncio
async def test_escape_closes_the_wizard_at_any_step(monkeypatch):
    monkeypatch.setattr(auth_mod.AuthProvider, "installed", lambda self: False)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app = DoxaApp(cwd=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_setup()
        await _open_setup(app, pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, setup_mod.SetupScreen)


# -- genuine first launch, end to end ---------------------------------------


@pytest.mark.asyncio
async def test_genuine_first_launch_auto_opens_setup_once(monkeypatch, tmp_path):
    monkeypatch.delenv("DOXA_SKIP_FIRST_RUN", raising=False)
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "fresh-doxa-home"))
    monkeypatch.setattr(auth_mod.AuthProvider, "installed", lambda self: False)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    config_mod.invalidate()
    assert setup_mod.needs_first_run() is True

    app = DoxaApp(cwd=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if isinstance(app.screen, setup_mod.SetupScreen):
                break
            await pilot.pause(0.02)
        assert isinstance(app.screen, setup_mod.SetupScreen)

    # The marker is written the moment it AUTO-triggers, not on completion.
    assert setup_mod.needs_first_run() is False

    # A second launch with the SAME home must not auto-open it again.
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    app2 = DoxaApp(cwd=".")
    async with app2.run_test() as pilot2:
        await pilot2.pause(0.3)
        assert not isinstance(app2.screen, setup_mod.SetupScreen)


@pytest.mark.asyncio
async def test_on_demand_setup_still_works_after_marker_is_set(monkeypatch, tmp_path):
    """/setup remains available any time, even once the auto-trigger is
    long spent."""
    monkeypatch.setattr(auth_mod.AuthProvider, "installed", lambda self: False)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    setup_mod.mark_seen()  # this test's own (conftest) DOXA_HOME, already "seen"
    app = DoxaApp(cwd=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not isinstance(app.screen, setup_mod.SetupScreen)
        app.action_setup()
        screen = await _open_setup(app, pilot)
        assert len(screen.findings) == 4

# SPDX-License-Identifier: AGPL-3.0-only
"""A saved model preference belongs to the engine that chose it."""

from __future__ import annotations

import pytest

from doxa import cli, config
from doxa.app import DoxaApp, SystemBlock
from doxa.settings import field_id
from tests.fakes import FakeEngine
from textual.widgets import Input


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.delenv("DOXA_MODEL", raising=False)
    monkeypatch.delenv("DOXA_ENGINE", raising=False)
    config.invalidate()
    yield
    config.invalidate()


def test_model_preferences_are_engine_scoped_and_legacy_model_is_claude():
    config.save({"engine": "codex", "model": "opus"})
    assert config.model("codex") is None
    assert config.model("claude") == "opus"

    config.save_model("codex", "gpt-6-sol")
    config.save_model("deepseek", "deepseek-flash")
    assert config.load()["model"] == "opus"
    assert config.load()["models"] == {
        "codex": "gpt-6-sol", "deepseek": "deepseek-flash",
    }
    assert config.model("codex") == "gpt-6-sol"
    assert config.model("deepseek") == "deepseek-flash"

    config.save_model("codex", "")
    assert config.model("codex") is None
    assert config.model("deepseek") == "deepseek-flash"
    assert config.model("claude") == "opus"


def test_explicit_model_and_env_override_engine_preference(monkeypatch, tmp_path):
    config.save({"engine": "codex", "model": "opus"})
    monkeypatch.chdir(tmp_path)
    spawned = []

    def fake_spawn(cwd, **kwargs):
        spawned.append(kwargs)
        return "sid", "/tmp/unused.sock"

    monkeypatch.setattr(cli, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(cli, "_run_attached", lambda *_args, **_kwargs: None)

    assert cli.main(["new", "--engine", "codex"]) == 0
    assert spawned[-1]["model"] is None
    config.save_model("codex", "gpt-6-sol")
    assert cli.main(["new", "--engine", "codex"]) == 0
    assert spawned[-1]["model"] == "gpt-6-sol"
    assert cli.main(["new", "--engine", "claude"]) == 0
    assert spawned[-1]["model"] == "opus"

    monkeypatch.setenv("DOXA_MODEL", "env-choice")
    assert cli.main(["new", "--engine", "codex"]) == 0
    assert spawned[-1]["model"] == "env-choice"
    assert cli.main(["new", "--engine", "codex", "--model", "flag-choice"]) == 0
    assert spawned[-1]["model"] == "flag-choice"


@pytest.mark.asyncio
async def test_settings_model_row_edits_the_active_engine(monkeypatch, tmp_path):
    config.save({"engine": "codex", "model": "opus"})
    config.save_model("codex", "gpt-6-sol")
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    fake = FakeEngine([], model="gpt-6-sol")
    fake.engine_id = "codex"
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        await pilot.pause()
        screen = app.screen
        model_field = screen.query_one(f"#{field_id('model')}", Input)
        assert model_field.value == "gpt-6-sol"
        model_field.value = "gpt-6-astra"
        screen.action_save()
        await pilot.pause()

    assert config.model("codex") == "gpt-6-astra"
    assert config.model("claude") == "opus"


@pytest.mark.asyncio
async def test_model_command_saves_only_the_active_engine(monkeypatch, tmp_path):
    config.save({"engine": "claude", "model": "opus"})
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    fake = FakeEngine([], model="gpt-6-sol")
    fake.engine_id = "codex"
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app.query(SystemBlock))
        app.query_one("#prompt-input").value = "/model gpt-6-astra"
        await pilot.press("enter")
        for _ in range(100):
            if len(app.query(SystemBlock)) > before:
                break
            await pilot.pause(0.02)

    assert fake.model == "gpt-6-astra"
    assert config.model("codex") == "gpt-6-astra"
    assert config.model("claude") == "opus"

# SPDX-License-Identifier: AGPL-3.0-only
"""/login and /logout delegate to provider CLIs while the TUI stays live."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from doxa import auth, cli_isolation, commands, config, identity
from doxa.app import DoxaApp, SystemBlock
from tests.fakes import FakeEngine


# -- the data table -------------------------------------------------------


def test_provider_table_rows_carry_all_three_commands():
    for name, row in auth.PROVIDERS.items():
        assert row.name == name
        assert row.login_cmd and row.logout_cmd and row.probe_cmd
        # Probed, not assumed: claude's verbs live under `claude auth`,
        # codex's are top level. A row is the only place that may differ.
        assert row.command_for("login") == row.login_cmd
        assert row.command_for("logout") == row.logout_cmd
        assert row.command_for("probe") == row.probe_cmd


def test_claude_and_codex_rows_match_the_probed_clis():
    assert auth.PROVIDERS["claude"].login_cmd == ("claude", "auth", "login")
    assert auth.PROVIDERS["claude"].logout_cmd == ("claude", "auth", "logout")
    assert auth.PROVIDERS["codex"].login_cmd == ("codex", "login")
    assert auth.PROVIDERS["codex"].logout_cmd == ("codex", "logout")


def test_resolve_defaults_to_claude(monkeypatch):
    monkeypatch.setattr(auth.shutil, "which", lambda _b: "/usr/bin/stub")
    assert auth.resolve(None).name == "claude"
    assert auth.resolve("").name == "claude"
    assert auth.resolve("  CODEX ").name == "codex"


def test_resolve_unknown_provider_lists_the_alternatives(monkeypatch):
    monkeypatch.setattr(auth.shutil, "which", lambda _b: "/usr/bin/stub")
    with pytest.raises(auth.AuthError) as excinfo:
        auth.resolve("gemini")
    message = str(excinfo.value)
    assert "gemini" in message
    for name in auth.provider_names():
        assert name in message


def test_resolve_absent_cli_says_which_providers_are_installed(monkeypatch):
    monkeypatch.setattr(
        auth.shutil, "which", lambda b: "/usr/bin/claude" if b == "claude" else None
    )
    with pytest.raises(auth.AuthError) as excinfo:
        auth.resolve("codex")
    message = str(excinfo.value)
    assert "codex" in message and "claude" in message


@pytest.mark.asyncio
async def test_auth_bridge_only_relays_public_browser_progress():
    output = []
    script = (
        "print('Visit https://auth.openai.com/oauth/authorize?state=example'); "
        "print('Device code: ABCD-1234'); "
        "print('access_token=secret-do-not-show')"
    )
    code = await auth.run_auth_command(
        (sys.executable, "-c", script), lambda line: _record(output, line),
    )
    assert code == 0
    assert any("auth.openai.com" in line for line in output)
    assert "Device code: ABCD-1234" in output
    assert all("secret-do-not-show" not in line for line in output)


@pytest.mark.asyncio
async def test_auth_bridge_times_out_and_stops_a_stuck_cli():
    code = await auth.run_auth_command(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        lambda line: _record([], line),
        timeout=0.1,
    )
    assert code == 124


def test_auth_progress_rejects_token_urls_and_unrelated_output():
    assert auth.public_progress(
        "https://auth.openai.com/callback?code=secret"
    ) is None
    assert auth.public_progress("refresh_token=secret") is None
    assert auth.public_progress("https://example.org/login") is None


async def _record(output, line):
    output.append(line)


# -- the TUI path ---------------------------------------------------------


async def _boot(app, pilot):
    for _ in range(200):
        if app.query("#identity-block"):
            return
        await pilot.pause(0.02)
    raise AssertionError("identity block never mounted")


def _system_texts(app) -> list[str]:
    return [b.text for b in app.query(SystemBlock) if b.id != "identity-block"]


@pytest.mark.asyncio
async def test_login_keeps_tui_live_and_refreshes_identity(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    config.invalidate()
    identity.invalidate()
    monkeypatch.setattr(auth.shutil, "which", lambda _b: "/usr/bin/stub")

    fake = FakeEngine([])
    fake.account = {"subscriptionType": "Claude Max", "email": "a@b.c"}
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)

    execs: list[tuple] = []
    started = asyncio.Event()
    finish = asyncio.Event()

    async def fake_exec(cmd, progress):
        execs.append(tuple(cmd))
        await progress("Open in your browser: https://claude.ai/oauth/authorize")
        started.set()
        await finish.wait()
        # The auth flow "signs in": a config with the precise tier appears.
        (tmp_path / ".claude.json").write_text(
            json.dumps({"oauthAccount": {
                "organizationRateLimitTier": "default_claude_max_20x",
                "organizationName": "Doc's Org",
            }}),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(auth, "run_auth_command", fake_exec)

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await _boot(app, pilot)

        app.query_one("#prompt-input").value = "/login"
        await pilot.press("enter")
        await asyncio.wait_for(started.wait(), 2)
        assert "browser" in "\n".join(_system_texts(app))
        # Auth is still waiting, but the prompt and event loop remain usable.
        app.query_one("#prompt-input").value = "still responsive"
        await pilot.pause(0.02)
        assert app.query_one("#prompt-input").value == "still responsive"
        finish.set()
        for _ in range(200):
            if "done" in "\n".join(_system_texts(app)):
                break
            await pilot.pause(0.02)

        assert execs == [("claude", "auth", "login")]

        # Identity was re-read: the precise tier now shows in BOTH surfaces.
        identity_text = app.query_one("#identity-block", SystemBlock).text
        assert "max 20x" in identity_text
        assert "max 20x" in str(app.query_one("#status-bar").content)
    identity.invalidate()
    config.invalidate()


@pytest.mark.asyncio
async def test_logout_uses_the_logout_row_and_reports_a_nonzero_exit(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    config.invalidate()
    identity.invalidate()
    monkeypatch.setattr(auth.shutil, "which", lambda _b: "/usr/bin/stub")
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))

    execs: list[tuple] = []

    async def fake_exec(cmd, _progress):
        execs.append(tuple(cmd))
        return 3

    monkeypatch.setattr(auth, "run_auth_command", fake_exec)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await _boot(app, pilot)
        app.query_one("#prompt-input").value = "/logout codex"
        await pilot.press("enter")
        for _ in range(200):
            if "exited 3" in "\n".join(_system_texts(app)):
                break
            await pilot.pause(0.02)
        assert execs == [("codex", "logout")]
        assert "exited 3" in "\n".join(_system_texts(app))
    identity.invalidate()


@pytest.mark.asyncio
async def test_successful_claude_logout_blocks_isolated_reimport(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real-claude"))
    (tmp_path / "real-claude").mkdir()
    config.invalidate()
    identity.invalidate()
    source = cli_isolation.user_credentials_path()
    source.write_text('{"claudeAiOauth": {"accessToken": "old"}}')
    assert cli_isolation.sync_credentials()
    monkeypatch.setattr(auth.shutil, "which", lambda _b: "/usr/bin/stub")
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))

    async def fake_exec(cmd, _progress):
        assert cmd == ("claude", "auth", "logout")
        return 0

    monkeypatch.setattr(auth, "run_auth_command", fake_exec)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await _boot(app, pilot)
        app.query_one("#prompt-input").value = "/logout claude"
        await pilot.press("enter")
        for _ in range(200):
            if "done" in "\n".join(_system_texts(app)):
                break
            await pilot.pause(0.02)
        assert "done" in "\n".join(_system_texts(app))
        assert not cli_isolation.isolated_credentials_path().exists()
        assert cli_isolation.sync_credentials() is False
    config.invalidate()
    identity.invalidate()


@pytest.mark.asyncio
async def test_codex_device_auth_is_explicit_and_other_flags_are_rejected(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    monkeypatch.setattr(auth.shutil, "which", lambda _b: "/usr/bin/stub")
    commands_run = []

    async def fake_exec(cmd, progress):
        commands_run.append(cmd)
        await progress("Device code: ABCD-1234")
        return 0

    monkeypatch.setattr(auth, "run_auth_command", fake_exec)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await _boot(app, pilot)
        app.query_one("#prompt-input").value = "/login codex --with-api-key"
        await pilot.press("enter")
        for _ in range(50):
            if "unsupported option" in "\n".join(_system_texts(app)):
                break
            await pilot.pause(0.02)
        assert commands_run == []

        app.query_one("#prompt-input").value = "/login codex --device-auth"
        await pilot.press("enter")
        for _ in range(100):
            if commands_run and "Device code" in "\n".join(_system_texts(app)):
                break
            await pilot.pause(0.02)
        assert commands_run == [("codex", "login", "--device-auth")]
        assert "Device code: ABCD-1234" in "\n".join(_system_texts(app))


@pytest.mark.asyncio
async def test_unknown_provider_never_execs(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))

    async def no_exec(_cmd, _progress):
        pytest.fail("no exec may happen for an unknown provider")
    monkeypatch.setattr(auth, "run_auth_command", no_exec)

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await _boot(app, pilot)
        app.query_one("#prompt-input").value = "/login gemini"
        await pilot.press("enter")
        for _ in range(200):
            if _system_texts(app):
                break
            await pilot.pause(0.02)

        text = _system_texts(app)[0]
        assert "gemini" in text and "claude" in text


# -- registry closure -----------------------------------------------------


@pytest.mark.asyncio
async def test_every_interactive_registry_row_has_exactly_one_handler(
    monkeypatch, tmp_path
):
    """Registry-closure discipline, the slash-command edition: the list in
    doxa/commands.py and the handler table on the pane are the same set --
    neither surface may grow a command the other doesn't have."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        assert set(pane._command_handlers()) == set(commands.interactive_names())


def test_login_and_logout_are_registered_commands():
    assert commands.find("/login") is not None
    assert commands.find("/logout") is not None
    assert commands.lookup("/login codex").name == "/login"
    assert commands.lookup("what is /login?") is None

"""/login and /logout: the provider table, and the suspend-exec-resume path.

DOXA never handles a credential -- it suspends the TUI and runs the
provider's own auth CLI. So what is worth testing is exactly: the table
resolves (and refuses, informatively, when it can't), the TUI really is
suspended around the exec, the exec is the row's command and nobody else's,
and identity is re-read on return. The exec itself is mocked throughout: no
test may open a browser or touch a real account.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from doxa import auth, commands, identity
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


def test_run_auth_command_survives_a_missing_binary(monkeypatch):
    def boom(_cmd):
        raise OSError("no such file")

    monkeypatch.setattr(auth.subprocess, "call", boom)
    assert auth.run_auth_command(("nope", "login")) == 127


# -- the TUI path ---------------------------------------------------------


class _SuspendRecorder:
    """Stands in for App.suspend(): records that the TUI was suspended, and
    that the exec happened INSIDE the suspension (never around it)."""

    def __init__(self) -> None:
        self.entered = 0
        self.inside = False

    @contextlib.contextmanager
    def __call__(self):
        self.entered += 1
        self.inside = True
        try:
            yield
        finally:
            self.inside = False


async def _boot(app, pilot):
    for _ in range(200):
        if app.query("#identity-block"):
            return
        await pilot.pause(0.02)
    raise AssertionError("identity block never mounted")


def _system_texts(app) -> list[str]:
    return [b.text for b in app.query(SystemBlock) if b.id != "identity-block"]


@pytest.mark.asyncio
async def test_login_suspends_the_tui_and_execs_the_provider_cli(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    identity.invalidate()
    monkeypatch.setattr(auth.shutil, "which", lambda _b: "/usr/bin/stub")

    fake = FakeEngine([])
    fake.account = {"subscriptionType": "Claude Max", "email": "a@b.c"}
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)

    execs: list[tuple] = []
    recorder = _SuspendRecorder()

    def fake_exec(cmd):
        # The exec must happen while the app is suspended -- otherwise the
        # child and the TUI fight over the terminal.
        assert recorder.inside is True
        execs.append(tuple(cmd))
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
        monkeypatch.setattr(type(app), "suspend", lambda _self: recorder())

        app.query_one("#prompt-input").value = "/login"
        await pilot.press("enter")
        for _ in range(200):
            if execs and _system_texts(app):
                break
            await pilot.pause(0.02)

        assert execs == [("claude", "auth", "login")]
        assert recorder.entered == 1

        # Identity was re-read: the precise tier now shows in BOTH surfaces.
        identity_text = app.query_one("#identity-block", SystemBlock).text
        assert "max 20x" in identity_text
        assert "max 20x" in str(app.query_one("#status-bar").renderable)
    identity.invalidate()


@pytest.mark.asyncio
async def test_logout_uses_the_logout_row_and_reports_a_nonzero_exit(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    identity.invalidate()
    monkeypatch.setattr(auth.shutil, "which", lambda _b: "/usr/bin/stub")
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))

    execs: list[tuple] = []
    recorder = _SuspendRecorder()
    monkeypatch.setattr(
        auth, "run_auth_command", lambda cmd: (execs.append(tuple(cmd)), 3)[1]
    )

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await _boot(app, pilot)
        monkeypatch.setattr(type(app), "suspend", lambda _self: recorder())

        app.query_one("#prompt-input").value = "/logout codex"
        await pilot.press("enter")
        for _ in range(200):
            if _system_texts(app):
                break
            await pilot.pause(0.02)

        assert execs == [("codex", "logout")]
        assert "exited 3" in _system_texts(app)[0]
    identity.invalidate()


@pytest.mark.asyncio
async def test_unknown_provider_never_suspends_or_execs(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))

    recorder = _SuspendRecorder()
    monkeypatch.setattr(
        auth, "run_auth_command",
        lambda cmd: pytest.fail("no exec may happen for an unknown provider"),
    )

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await _boot(app, pilot)
        monkeypatch.setattr(type(app), "suspend", lambda _self: recorder())

        app.query_one("#prompt-input").value = "/login gemini"
        await pilot.press("enter")
        for _ in range(200):
            if _system_texts(app):
                break
            await pilot.pause(0.02)

        text = _system_texts(app)[0]
        assert "gemini" in text and "claude" in text
        assert recorder.entered == 0


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

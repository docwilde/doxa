# SPDX-License-Identifier: AGPL-3.0-only
"""The welcome account and utilization belong to the active engine."""

from __future__ import annotations

import json
import sys
import textwrap

import pytest
from textual.widgets import Static

from doxa import codex_account, config, identity
from doxa.app import DoxaApp, SystemBlock
from doxa.codex import CodexEngine
from doxa.settings import SettingsScreen
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _account_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "emailAddress": "claude@example.com",
        "organizationName": "Claude Org",
        "organizationRole": "owner",
        "organizationRateLimitTier": "default_claude_max_20x",
        "cachedUsageUtilization": {
            "fiveHour": {"utilization": 0.42},
        },
    }}))
    identity.invalidate()
    config.invalidate()
    yield
    identity.invalidate()
    config.invalidate()


async def _opened(app, pilot):
    for _ in range(200):
        if app.query("#identity-block"):
            return
        await pilot.pause(0.02)
    raise AssertionError("identity block never mounted")


@pytest.mark.asyncio
async def test_claude_banner_preserves_precise_plan_and_organization(tmp_path):
    fake = FakeEngine([])
    fake.account = {"email": "claude@example.com", "subscriptionType": "Claude Max"}
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: fake)
    async with app.run_test() as pilot:
        await _opened(app, pilot)
        banner = app.query_one("#identity-block", SystemBlock).text
        assert "DOXA 1.18.0" in banner
        assert "account  claude@example.com" in banner
        assert "plan     max 20x" in banner
        assert "org      Claude Org (owner)" in banner
        assert "max 20x" in app.active_pane._usage_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [
    {"type": "chatgpt", "email": "codex@example.com", "planType": "prolite"},
    {},
])
async def test_codex_banner_and_usage_never_inherit_claude_identity(
    tmp_path, account,
):
    fake = FakeEngine([], model="gpt-6-sol")
    fake.engine_id = "codex"
    fake.account = account
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: fake)
    async with app.run_test() as pilot:
        await _opened(app, pilot)
        banner = app.query_one("#identity-block", SystemBlock).text
        usage = app.active_pane._usage_text()
        assert "DOXA 1.18.0" in banner
        assert "Claude Org" not in banner
        assert "claude@example.com" not in banner
        assert "max 20x" not in banner + usage
        assert "claude CLI" not in usage
        assert "session (5h)" not in usage
        app.active_pane._refresh_usage_chip()
        assert app.active_pane._usage_chip is None
        if account:
            assert "account  codex@example.com" in banner
            assert "plan     ChatGPT prolite" in banner
            assert "ChatGPT prolite" in usage
        else:
            assert "account  " not in banner
            assert "plan     " not in banner


@pytest.mark.asyncio
async def test_codex_reads_only_its_own_app_server_account(tmp_path):
    script = tmp_path / "account-server.py"
    log = tmp_path / "requests.jsonl"
    script.write_text(textwrap.dedent("""
        import json
        import sys

        log = open(sys.argv[1], "w")
        for line in sys.stdin:
            request = json.loads(line)
            log.write(json.dumps(request) + "\\n")
            log.flush()
            if request.get("method") == "initialize":
                print(json.dumps({"id": request["id"], "result": {}}), flush=True)
            elif request.get("method") == "account/read":
                print(json.dumps({"id": request["id"], "result": {"account": {
                    "type": "chatgpt", "email": "codex@example.com",
                    "planType": "prolite", "secret": "never-display",
                }}}), flush=True)
    """))
    account = await codex_account.read_account(
        (sys.executable, "-u", str(script), str(log)), timeout=2.0,
    )
    assert account == {
        "type": "chatgpt", "email": "codex@example.com", "planType": "prolite",
    }
    requests = [json.loads(line) for line in log.read_text().splitlines()]
    assert [request["method"] for request in requests] == [
        "initialize", "initialized", "account/read",
    ]
    assert requests[-1]["params"] == {"refreshToken": False}


@pytest.mark.asyncio
async def test_unresponsive_codex_account_probe_leaves_banner_fields_absent(tmp_path):
    script = tmp_path / "slow-account-server.py"
    script.write_text("import time\ntime.sleep(2)\n")
    assert await codex_account.read_account(
        (sys.executable, "-u", str(script)), timeout=0.05,
    ) == {}


@pytest.mark.asyncio
async def test_codex_start_exposes_fetched_account(tmp_path, monkeypatch):
    monkeypatch.setattr("doxa.codex.shutil.which", lambda _name: "/usr/bin/codex")

    async def fetch():
        return {"type": "chatgpt", "email": "codex@example.com", "planType": "prolite"}

    engine = CodexEngine(cwd=str(tmp_path), exec_factory=lambda *a, **kw: None,
                         account_fetch=fetch)
    try:
        await engine.start()
        assert engine.account["email"] == "codex@example.com"
    finally:
        await engine.finalize()


def test_settings_about_uses_only_the_selected_engines_account():
    def values(screen):
        return [str(row.renderable) for row in screen._about_rows()
                if isinstance(row, Static)]

    claude = values(SettingsScreen(
        session_engine="claude", account={"subscriptionType": "Claude Max"},
    ))
    assert "max 20x" in claude
    assert "Claude Org" in claude

    codex = values(SettingsScreen(
        session_engine="codex", account={
            "type": "chatgpt", "email": "codex@example.com",
            "planType": "prolite",
        },
    ))
    assert "codex@example.com" in codex
    assert "ChatGPT prolite" in codex
    assert "Claude Org" not in codex
    assert "max 20x" not in codex

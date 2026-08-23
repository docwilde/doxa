"""The Claude-Code-shaped commands: /model /effort /usage /clear /compact /help.

Each of these was implemented only as far as the SDK actually supports it,
and these tests pin exactly that line:

* ``/model`` is REAL and live -- ``ClaudeSDKClient.set_model`` is a control
  request, so the transcript, the daemon and the replay ring survive a
  switch. Tested through the engine surface the app calls.
* ``/effort`` is real but CONNECT-TIME: ``ClaudeAgentOptions.effort`` (the
  CLI's --effort) has no control-request counterpart, so the command sets
  the level for NEW sessions and says so. Tested for both halves: the
  option genuinely reaches the SDK options, and the command does not claim
  to change the running session.
* ``/usage`` reports measured numbers only -- the CLI's own per-result
  token block and the utilization snapshot the CLI itself cached.
* ``/clear`` starts a fresh session in the tab (distinct from a new tab).
* ``/compact`` is registered but deliberately NOT intercepted: the literal
  prompt text is what triggers compaction and fires the PreCompact hook.
* ``/help`` is generated from the registry.
"""

from __future__ import annotations

import json

import pytest

from doxa import commands, config, engine, identity
from doxa.app import DoxaApp, SystemBlock, help_text
from doxa.engine import EngineEvent, SessionEngine
from tests.fakes import FakeEngine, factory_with_script


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    config.invalidate()
    yield
    config.invalidate()


def _system_texts(app) -> list[str]:
    return [b.text for b in app.query(SystemBlock) if b.id != "identity-block"]


async def _run(app, pilot, line: str) -> str:
    app.query_one("#prompt-input").value = line
    before = len(_system_texts(app))
    await pilot.press("enter")
    for _ in range(200):
        texts = _system_texts(app)
        if len(texts) > before:
            return texts[-1]
        await pilot.pause(0.02)
    raise AssertionError(f"{line!r} produced no output block")


async def _app(monkeypatch, tmp_path, fake=None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    fake = fake or FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    return app, fake


# -- /model ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_lists_aliases_and_marks_the_current_one(monkeypatch, tmp_path):
    fake = FakeEngine([], model="claude-sonnet-4-5")
    app, fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/model")
        assert "claude-sonnet-4-5" in text
        assert "▸ sonnet" in text
        assert "haiku" in text and "opus" in text
        assert fake.model_switches == []  # listing never switches


@pytest.mark.asyncio
async def test_model_switch_is_live_and_becomes_the_settings_row(
    monkeypatch, tmp_path
):
    """One source of truth: /model switches the live session AND updates
    the settings file's model row, so the menu never disagrees with the
    session."""
    fake = FakeEngine([], model="claude-sonnet-4-5")
    app, fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/model haiku")
        assert fake.model_switches == ["haiku"]
        assert fake.model == "haiku"
        assert "no reconnect" in text
        assert "haiku" in str(app.query_one("#status-bar").renderable)
    config.invalidate()
    assert config.load()["model"] == "haiku"
    assert config.model() == "haiku"


@pytest.mark.asyncio
async def test_model_switch_failure_is_reported_not_swallowed(
    monkeypatch, tmp_path
):
    class Refusing(FakeEngine):
        async def set_model(self, model):
            raise RuntimeError("session is not connected")

    app, _fake = await _app(monkeypatch, tmp_path, Refusing([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/model opus")
        assert "session is not connected" in text


def test_engine_set_model_uses_the_sdk_control_request():
    """The engine's half: set_model goes to the SDK client's own method --
    the thing that makes a switch a switch and not a reconnect."""

    class Client:
        def __init__(self):
            self.models = []

        async def set_model(self, model):
            self.models.append(model)

    import asyncio

    eng = SessionEngine(cwd=".", model="claude-sonnet-4-5")
    client = Client()
    eng._client = client
    eng._connected = True
    assert asyncio.run(eng.set_model("haiku")) == "haiku"
    assert client.models == ["haiku"]
    assert eng.model == "haiku"


def test_engine_set_model_refuses_when_not_connected():
    import asyncio

    eng = SessionEngine(cwd=".")
    with pytest.raises(RuntimeError):
        asyncio.run(eng.set_model("haiku"))


# -- /effort --------------------------------------------------------------


def test_effort_level_reads_config_and_rejects_unknown_values(monkeypatch):
    monkeypatch.delenv("DOXA_EFFORT", raising=False)
    assert engine.effort_level() is None
    config.save({"effort": "high"})
    assert engine.effort_level() == "high"
    config.save({"effort": ""})
    monkeypatch.setenv("DOXA_EFFORT", "turbo")  # not a level the SDK has
    assert engine.effort_level() is None


def test_effort_reaches_the_sdk_options(monkeypatch):
    """The option is genuinely wired: a configured level shows up on the
    ClaudeAgentOptions the engine connects with."""
    monkeypatch.setenv("DOXA_EFFORT", "xhigh")
    eng = SessionEngine(cwd=".")
    options = eng._build_options()
    assert options.effort == "xhigh"

    monkeypatch.delenv("DOXA_EFFORT", raising=False)
    config.invalidate()
    assert SessionEngine(cwd=".")._build_options().effort is None


@pytest.mark.asyncio
async def test_effort_command_does_not_claim_to_change_this_session(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("DOXA_EFFORT", raising=False)
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/effort high")
        assert "new sessions" in text
        assert "no live setter" in text
    config.invalidate()
    assert config.load()["effort"] == "high"


@pytest.mark.asyncio
async def test_effort_rejects_a_level_the_sdk_does_not_have(monkeypatch, tmp_path):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/effort turbo")
        assert "unknown level" in text
        for level in engine.EFFORT_LEVELS:
            assert level in text


# -- /usage ---------------------------------------------------------------


def _write_usage(tmp_path, session_pct=9, weekly_pct=48, fetched_ms=None):
    import time

    payload = {
        "oauthAccount": {"organizationRateLimitTier": "default_claude_max_20x"},
        "cachedUsageUtilization": {
            "fetchedAtMs": fetched_ms if fetched_ms is not None
            else int(time.time() * 1000),
            "utilization": {
                "limits": [
                    {"kind": "session", "percent": session_pct,
                     "severity": "normal",
                     "resets_at": "2026-08-23T22:49:59.561178+00:00"},
                    {"kind": "weekly_all", "percent": weekly_pct,
                     "severity": "normal",
                     "resets_at": "2026-08-28T13:59:59.561234+00:00"},
                    {"kind": "weekly_scoped", "percent": 76,
                     "severity": "warning",
                     "resets_at": "2026-08-28T13:59:59.561512+00:00",
                     "scope": {"model": {"display_name": "Fable"}}},
                ],
            },
        },
    }
    (tmp_path / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")


def test_usage_snapshot_parses_the_cli_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    identity.invalidate()
    _write_usage(tmp_path)
    usage = identity.usage()
    assert usage is not None
    assert usage.session.percent == 9
    assert usage.weekly.percent == 48
    assert usage.scoped.percent == 76 and usage.scoped.severity == "warning"
    assert usage.scope_label == "Fable"
    assert usage.is_stale() is False
    assert usage.chip() == "s:9% w:48% fable:76%"
    identity.invalidate()


def test_usage_snapshot_is_none_without_a_cache(monkeypatch, tmp_path):
    """Nothing cached means nothing shown -- never a fabricated zero."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    identity.invalidate()
    assert identity.usage() is None
    identity.invalidate()


def test_usage_snapshot_marks_a_stale_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    identity.invalidate()
    _write_usage(tmp_path, fetched_ms=0)  # 1970
    usage = identity.usage()
    assert usage.is_stale() is True
    assert usage.chip().endswith("~")
    identity.invalidate()


@pytest.mark.asyncio
async def test_usage_command_shows_session_and_account_numbers(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    identity.invalidate()
    _write_usage(tmp_path)
    fake = FakeEngine([])
    fake.account = {"subscriptionType": "Claude Max"}
    fake.num_turns = 4
    app, _fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/usage")
        assert "turns" in text and "4" in text
        assert "1,200" in text and "340" in text  # the CLI's own token counts
        assert "max 20x" in text  # the precise plan, not a coarse string
        assert "session (5h)" in text and "9%" in text
        assert "weekly" in text and "48%" in text
        assert "warning" in text  # the scoped window's real severity
    identity.invalidate()


@pytest.mark.asyncio
async def test_usage_says_so_when_no_utilization_is_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    identity.invalidate()
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/usage")
        assert "no subscription utilization cached" in text
        assert "%" not in text.split("no subscription")[1]  # nothing invented
    identity.invalidate()


def test_engine_totals_come_from_the_result_message(monkeypatch, tmp_path):
    """The token numbers /usage shows are the CLI's own, summed -- not an
    estimate DOXA computed."""
    import asyncio

    from claude_agent_sdk import ResultMessage

    result = ResultMessage(
        subtype="success", duration_ms=10, duration_api_ms=8, is_error=False,
        num_turns=1, session_id="s", total_cost_usd=0.01,
        usage={"input_tokens": 11, "output_tokens": 22,
               "cache_read_input_tokens": 33},
    )
    factory, _created = factory_with_script([result])
    eng = SessionEngine(cwd=str(tmp_path), client_factory=factory)

    async def drive():
        await eng.start()
        async for _ev in eng.send("hi"):
            pass

    asyncio.run(drive())
    summary = eng.usage_summary()
    assert summary["input_tokens"] == 11
    assert summary["output_tokens"] == 22
    assert summary["cache_read_input_tokens"] == 33
    assert summary["num_turns"] == 1


# -- /clear ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_starts_a_fresh_session_in_the_same_tab(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    engines: list[FakeEngine] = []

    def make_engine():
        engines.append(FakeEngine([]))
        return engines[-1]

    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=make_engine, new_session_factory=make_engine
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.panes()) == 1
        app.query_one("#prompt-input").value = "/clear"
        await pilot.press("enter")
        for _ in range(200):
            if len(engines) == 2 and engines[1].started:
                break
            await pilot.pause(0.02)
        assert len(engines) == 2
        assert engines[0].finalized is True   # the old session was finalized
        assert app.engine is engines[1]
        assert len(app.panes()) == 1          # ...in the SAME tab


# -- /compact and /help ---------------------------------------------------


def test_compact_is_registered_but_never_intercepted():
    command = commands.find("/compact")
    assert command is not None and command.passthrough is True
    assert "/compact" not in commands.interactive_names()


@pytest.mark.asyncio
async def test_compact_reaches_the_model_as_a_prompt(monkeypatch, tmp_path):
    """The literal "/compact" text IS the trigger (PHASE0 §2/§6) -- if DOXA
    intercepted it, compaction and the PreCompact-hook review would both
    stop happening."""
    script = [EngineEvent("turn_started", {}), EngineEvent("turn_done", {})]
    app, fake = await _app(monkeypatch, tmp_path, FakeEngine(script))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "/compact"
        await pilot.press("enter")
        from doxa.app import TurnBlock

        for _ in range(200):
            if list(app.query(TurnBlock)):
                break
            await pilot.pause(0.02)
        blocks = list(app.query(TurnBlock))
        assert len(blocks) == 1
        assert blocks[0].prompt_text == "/compact"


def test_help_is_generated_from_the_registry():
    text = help_text()
    for command in commands.REGISTRY:
        assert command.call_form() in text
        assert command.summary in text
    assert "not intercepted" in text  # the passthrough row says what it is


@pytest.mark.asyncio
async def test_help_command_renders(monkeypatch, tmp_path):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/help")
        assert "/usage" in text and "/model" in text

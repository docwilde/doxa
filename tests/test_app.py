"""Headless Textual pilot test: mounts DoxaApp with a scripted FakeEngine
(no real SDK client underneath), submits one prompt, and asserts the turn
block + tool chip appear live and the chip's body lazily formats on first
expand. Follows PHASE0_FINDINGS.md SS4's proven pattern (run_test()'s Pilot
harness, no real terminal needed) -- same shape as spike/03_textual_marriage.py.
"""

from __future__ import annotations

import pytest

from doxa.app import ClockChip, DoxaApp, SystemBlock, ToolChip, TurnBlock
from doxa.engine import EngineEvent
from tests.fakes import FakeEngine

SCRIPT = [
    EngineEvent("turn_started", {}),
    EngineEvent("text_delta", {"text": "The answer is "}),
    EngineEvent("text_delta", {"text": "4."}),
    EngineEvent("tool_call", {"id": "t1", "name": "calculator_add", "input": {"a": 2, "b": 2}}),
    EngineEvent("tool_result", {
        "id": "t1", "name": "calculator_add", "result_summary": "4",
        "is_error": False, "duration_ms": 12,
    }),
    EngineEvent("turn_done", {
        "cost_usd": 0.002, "duration_ms": 250, "is_error": False,
        "session_cost_usd": 0.002, "ctx_percentage": 8.0,
    }),
]


@pytest.mark.asyncio
async def test_turn_block_and_tool_chip_appear_live(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: FakeEngine(SCRIPT),
    )

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.engine is not None and app.engine.started is True

        app.query_one("#prompt-input").value = "what is 2+2?"
        await pilot.press("enter")

        for _ in range(100):
            blocks = list(app.query(TurnBlock))
            if blocks and blocks[0].assistant_text == "The answer is 4.":
                break
            await pilot.pause(0.02)

        turn_blocks = list(app.query(TurnBlock))
        assert len(turn_blocks) == 1
        block = turn_blocks[0]
        assert block.assistant_text == "The answer is 4."
        assert block.prompt_text == "what is 2+2?"

        chips = list(app.query(ToolChip))
        assert len(chips) == 1
        chip = chips[0]
        assert chip.tool_name == "calculator_add"
        assert chip.tool_result == "4"
        assert chip.is_error is False
        assert chip.collapsed is True
        # Lazy formatting: the body must not be formatted before the chip
        # is ever expanded.
        assert chip._formatted is False

        chip.collapsed = False
        await pilot.pause()
        assert chip._formatted is True
        assert "ARGS:" in chip._body.renderable
        assert "RESULT:\n4" in chip._body.renderable

        # Status bar reflects the finished turn.
        status = app.query_one("#status-bar").renderable
        assert "0.0020" in status or "$0.0020" in status
        assert "3 beliefs" in status


@pytest.mark.asyncio
async def test_no_live_animation_timers_after_turns(monkeypatch, tmp_path):
    """Idle-CPU regression: a finished turn's hidden LoadingIndicator must
    not keep its 16Hz auto-refresh timer alive -- one leaked timer per turn
    made idle CPU grow linearly with scrollback. After N completed turns,
    zero auto-refresh timers may remain armed anywhere in the DOM."""
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: FakeEngine(SCRIPT),
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        for i in range(3):
            app.query_one("#prompt-input").value = f"turn {i}"
            await pilot.press("enter")
            for _ in range(100):
                blocks = list(app.query(TurnBlock))
                if len(blocks) == i + 1 and blocks[i].assistant_text:
                    break
                await pilot.pause(0.02)
        assert len(list(app.query(TurnBlock))) == 3
        # ClockChip (item M) is the ONE permitted standing timer -- see
        # tests/test_chrome.py's _armed docstring for why excluding it is
        # not a widening of this guard. Everything else must still be bare.
        armed = [
            node for node in app.query("*")
            if not isinstance(node, ClockChip)
            and getattr(node, "_auto_refresh_timer", None) is not None
        ]
        assert armed == []
        for block in app.query(TurnBlock):
            assert block.thinking.display is False
            assert block.thinking.auto_refresh is None


@pytest.mark.asyncio
async def test_tool_disabled_shows_in_status_area(monkeypatch, tmp_path):
    """Two-strikes containment is visible: the tool_disabled event mounts a
    system block and the status bar carries the small `⊘ toolname` note."""
    fake = FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        fake.disabled = ["lore_belief_search"]
        fake.push_peer_event(EngineEvent("tool_disabled", {
            "name": "lore_belief_search",
            "reason": "lore_belief_search failed: RuntimeError: belief db unavailable",
        }))

        def _blocks():
            # ignore the session-start identity block
            return [b for b in app.query(SystemBlock) if b.id != "identity-block"]

        for _ in range(100):
            if _blocks():
                break
            await pilot.pause(0.02)

        blocks = _blocks()
        assert len(blocks) == 1
        assert "⊘" in blocks[0].text
        assert "lore_belief_search" in blocks[0].text

        status = str(app.query_one("#status-bar").renderable)
        assert "⊘ lore_belief_search" in status


def test_tier_short_forms():
    from doxa.app import tier_short

    assert tier_short("Claude Max") == "max"
    assert tier_short("Claude Pro") == "pro"
    assert tier_short("Team") == "team"
    assert tier_short(None) is None
    assert tier_short("   ") is None


@pytest.mark.asyncio
async def test_subscription_auth_shows_tier_not_dollars(monkeypatch, tmp_path):
    """On subscription auth the status line leads with sub:<tier>; the
    list-price figure survives only as the explicit '≈$ if API' what-if.
    The identity block renders the REAL account fields."""
    fake = FakeEngine([])
    fake.account = {
        "email": "doc@example.org", "organization": "Doc's Org",
        "subscriptionType": "Claude Max", "apiProvider": "firstParty",
    }
    fake.lore_root = "/fake/lore"
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        for _ in range(100):
            if app.query("#identity-block"):
                break
            await pilot.pause(0.02)
        status = str(app.query_one("#status-bar").renderable)
        assert "sub:max" in status
        assert "(≈$0.0000 if API)" in status  # secondary what-if, not a bill

        identity = app.query_one("#identity-block", SystemBlock)
        text = identity.text
        assert "doc@example.org" in text and "Doc's Org" in text
        assert "Claude Max" in text and "firstParty" in text
        assert fake.model in text
        assert str(tmp_path) in text
        assert "/fake/lore" in text and "3 beliefs" in text


@pytest.mark.asyncio
async def test_api_key_auth_keeps_the_dollar_display(monkeypatch, tmp_path):
    """No subscription tier reported (API-key auth): the plain $ figure
    stays, no sub: chip appears."""
    fake = FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        status = str(app.query_one("#status-bar").renderable)
        assert "$0.0000" in status
        assert "sub:" not in status and "if API" not in status


@pytest.mark.asyncio
async def test_status_line_shows_repo_and_branch(monkeypatch, tmp_path):
    """Inside a repo the status line carries ` <repo> ⎇ <branch>`, detected
    from a real tmp-repo fixture -- and a branch switch shows up on the next
    event-driven refresh (no polling anywhere)."""
    import subprocess

    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)

    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([])
    )
    app = DoxaApp(cwd=str(repo))
    async with app.run_test() as pilot:
        for _ in range(100):
            if app._git is not None:
                break
            await pilot.pause(0.02)
        status = str(app.query_one("#status-bar").renderable)
        assert "myrepo ⎇ trunk" in status

        # Branch switch: .git/HEAD changes; the next refresh must see it.
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-q", "-b", "feature/x"],
            check=True,
        )
        app._git._mtime = None  # defeat same-second mtime granularity
        app._refresh_status()
        status = str(app.query_one("#status-bar").renderable)
        assert "myrepo ⎇ feature/x" in status


@pytest.mark.asyncio
async def test_status_line_has_no_git_chip_outside_a_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([])
    )
    plain = tmp_path / "plain"
    plain.mkdir()
    app = DoxaApp(cwd=str(plain))
    async with app.run_test() as pilot:
        for _ in range(100):
            if app._git is not None:
                break
            await pilot.pause(0.02)
        assert app._git.render() is None
        status = str(app.query_one("#status-bar").renderable)
        assert "⎇" not in status


@pytest.mark.asyncio
async def test_ctrl_c_quits_via_detach_path(monkeypatch, tmp_path):
    """One Ctrl+C = quit-detach: after the double-press window expires the
    app finalizes the engine handle (detach over a daemon client, full
    finalize in-process) and exits -- Textual's default 'ctrl+c does not
    quit' behavior must never win over the priority binding."""
    from doxa.app import CTRL_C_DOUBLE_SECS

    fake = FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")
        # Window armed, not yet detached -- the second-press upgrade to
        # quit-stop must still be possible here.
        assert fake.finalized is False
        assert app._ctrl_c_timer is not None
        await pilot.pause(CTRL_C_DOUBLE_SECS + 0.5)
    assert fake.finalized is True


@pytest.mark.asyncio
async def test_double_ctrl_c_stops_the_session(monkeypatch, tmp_path):
    """Ctrl+C twice inside the window = quit-stop: the engine's stop()
    (finalize the daemon NOW) runs instead of the detach-only finalize()."""
    class StoppableEngine(FakeEngine):
        def __init__(self):
            super().__init__([])
            self.stopped = False

        async def stop(self):
            self.stopped = True

    engine = StoppableEngine()
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: engine)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.press("ctrl+c")
        await pilot.pause()
    assert engine.stopped is True
    assert engine.finalized is False  # stop path, never detach-finalize


@pytest.mark.asyncio
async def test_quit_finalizes_the_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: FakeEngine([]),
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        engine = app.engine
        assert engine is not None and engine.finalized is False
        await app.action_quit()
        assert engine.finalized is True

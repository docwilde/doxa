"""Item Q -- the ``!`` prefix: run a shell command from the DOXA prompt.

Two halves, and the second is the important one.

**What the user sees.** A ``!`` line runs in the session's own directory,
its output lands in the transcript as a block that cannot be mistaken for
the model's words, the exit code is always shown, a slow command leaves the
UI live, a runaway is capped and a hung one is killed.

**What must never happen.** ``doxa.shell.run`` executes arbitrary commands
with the user's full privileges. It is safe for exactly one reason: the
only thing that can reach it is a keystroke typed at the prompt. The
``security`` section below asserts that as a property of the code rather
than trusting it as a convention -- the model cannot reach it, a peer
session cannot reach it, the slash registry cannot reach it, and no module
the model's traffic passes through even imports it.
"""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path

import pytest

from doxa import commands, operators, shell
from doxa.app import DoxaApp, PeerMessageBlock, ShellBlock, SystemBlock
from doxa.engine import EngineEvent
from tests.fakes import FakeEngine

PACKAGE = Path(shell.__file__).parent


async def _app(monkeypatch, tmp_path, fake=None, cwd=None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    fake = fake if fake is not None else FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    return DoxaApp(cwd=str(cwd or tmp_path)), fake


async def _bang(app, pilot, line: str, timeout: float = 8.0) -> ShellBlock:
    """Type a ``!`` line, press enter, and wait for the block to FINISH.
    Returns the block itself so the caller can read what is on screen."""
    app.query_one("#prompt-input").value = line
    before = len(list(app.query(ShellBlock)))
    await pilot.press("enter")
    for _ in range(int(timeout / 0.02)):
        blocks = list(app.query(ShellBlock))
        if len(blocks) > before and blocks[-1].result is not None:
            return blocks[-1]
        await pilot.pause(0.02)
    raise AssertionError(f"{line!r} produced no finished shell block")


# -- what the user sees ---------------------------------------------------


@pytest.mark.asyncio
async def test_bang_renders_the_output_and_the_exit_code(monkeypatch, tmp_path):
    """The whole feature in one assertion: what the command printed is on
    screen, and so is how it ended."""
    app, fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _bang(app, pilot, "!echo hello-from-the-shell")
        rendered = str(block.renderable)
        assert "hello-from-the-shell" in rendered
        assert "exit 0" in rendered
        assert "echo hello-from-the-shell" in rendered  # the command itself


@pytest.mark.asyncio
async def test_a_failing_command_shows_its_real_exit_code(monkeypatch, tmp_path):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _bang(app, pilot, "!exit 42")
        assert "exit 42" in str(block.renderable)


@pytest.mark.asyncio
async def test_stderr_is_shown_too(monkeypatch, tmp_path):
    """A command's diagnostics are usually the interesting half; losing
    them would make a failure look like silence."""
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _bang(app, pilot, "!echo trouble >&2; exit 3")
        rendered = str(block.renderable)
        assert "trouble" in rendered and "exit 3" in rendered


@pytest.mark.asyncio
async def test_a_silent_command_still_says_how_it_ended(monkeypatch, tmp_path):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _bang(app, pilot, "!true")
        rendered = str(block.renderable)
        assert "no output" in rendered and "exit 0" in rendered


@pytest.mark.asyncio
async def test_it_runs_in_the_sessions_own_directory(monkeypatch, tmp_path):
    """Not wherever DOXA was launched from: with worktree-per-session on,
    the session's directory is its linked worktree, and `!git status` has
    to report on the tree the model is editing."""
    worktree = tmp_path / "session-worktree"
    worktree.mkdir()
    (worktree / "marker-file").write_text("x")
    launched_from = tmp_path / "launched-from"
    launched_from.mkdir()
    app, _fake = await _app(
        monkeypatch, tmp_path,
        fake=FakeEngine([], cwd=str(worktree)),
        cwd=launched_from,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _bang(app, pilot, "!ls")
        assert "marker-file" in str(block.renderable)


@pytest.mark.asyncio
async def test_a_slow_command_leaves_the_ui_live(monkeypatch, tmp_path):
    """The block appears in its RUNNING state immediately and the prompt
    still accepts keystrokes while the process is alive -- the Textual
    worker requirement, asserted from the user's side rather than by
    inspecting the worker."""
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "!sleep 1.5"
        await pilot.press("enter")
        for _ in range(200):
            blocks = list(app.query(ShellBlock))
            if blocks:
                break
            await pilot.pause(0.02)
        assert blocks, "no block mounted while the command was still running"
        assert blocks[0].result is None
        assert "running" in str(blocks[0].renderable)

        # The UI is not blocked: a keystroke still reaches the prompt.
        await pilot.press("h", "i")
        assert app.query_one("#prompt-input").value == "hi"

        for _ in range(400):
            if blocks[0].result is not None:
                break
            await pilot.pause(0.02)
        assert blocks[0].result is not None
        assert "exit 0" in str(blocks[0].renderable)


@pytest.mark.asyncio
async def test_bare_bang_explains_itself_instead_of_running_a_shell(
    monkeypatch, tmp_path
):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "!   "
        await pilot.press("enter")
        for _ in range(200):
            notes = [
                b for b in app.query(SystemBlock)
                if b.id != "identity-block" and "shell:" in b.text
            ]
            if notes:
                break
            await pilot.pause(0.02)
        assert notes, "a bare ! said nothing"
        assert "never reaches the model" in notes[0].text
        assert list(app.query(ShellBlock)) == []


@pytest.mark.asyncio
async def test_shell_block_is_not_styled_as_model_output(monkeypatch, tmp_path):
    """A distinct block kind, visibly: no `▎ doxa` system prefix, its own
    class, and its own theme rule. Confusing shell output with the
    assistant's words is the failure this styling exists to prevent."""
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _bang(app, pilot, "!echo x")
        assert not isinstance(block, SystemBlock)
        assert "shell-block" in block.classes
        assert "▎ doxa" not in str(block.renderable)
        assert str(block.renderable).startswith("❯ ")
    theme = (PACKAGE / "theme.tcss").read_text()
    assert "ShellBlock {" in theme


# -- capping and containment ---------------------------------------------


@pytest.mark.asyncio
async def test_runaway_output_is_capped_and_says_by_how_much(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(shell, "OUTPUT_CAP_BYTES", 100)
    result = await shell.run("yes x | head -c 5000", str(tmp_path))
    assert result.exit_code == 0
    assert len(result.output) == 100
    assert result.truncated is True
    assert result.dropped_bytes == 4900
    assert "4,900 more bytes not shown" in _completed_text(result)


def _completed_text(result) -> str:
    block = ShellBlock(result.command, result.cwd)
    block.complete(result)
    return str(block.renderable)


@pytest.mark.asyncio
async def test_a_hung_command_is_killed_with_its_whole_process_group(tmp_path):
    """`!tail -f` typed by mistake must not outlive the tab, and killing
    only the `sh` would orphan whatever it spawned -- which is why the
    child gets its own session and the kill goes to the group."""
    pidfile = tmp_path / "child.pid"
    result = await shell.run(
        f"sleep 60 & echo $! > {pidfile}; wait", str(tmp_path), timeout=0.6,
    )
    assert result.timed_out is True
    assert result.exit_code is None
    assert "killed after" in result.status_line()
    assert result.duration_ms < 10_000

    child = int(pidfile.read_text().strip())
    for _ in range(100):
        try:
            os.kill(child, 0)
        except (ProcessLookupError, PermissionError):
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover -- only on a real leak
        raise AssertionError(f"grandchild {child} survived the timeout kill")


@pytest.mark.asyncio
async def test_a_command_that_cannot_start_reports_it_without_an_exit_code(
    tmp_path
):
    """No invented status: a shell that never ran has no exit code, and the
    result says so rather than reporting 0."""
    result = await shell.run("echo hi", str(tmp_path / "does-not-exist"))
    assert result.exit_code is None
    assert "exit ?" in result.status_line()
    assert result.output


@pytest.mark.asyncio
async def test_stdin_is_closed_so_an_interactive_command_cannot_hang(tmp_path):
    """A TUI has no terminal to hand over mid-command. Without /dev/null on
    stdin, `!cat` waits forever on input that can never arrive."""
    result = await shell.run("cat", str(tmp_path), timeout=5.0)
    assert result.timed_out is False
    assert result.exit_code == 0


# -- security -------------------------------------------------------------
#
# doxa.shell.run executes arbitrary commands with the user's full
# privileges. Every assertion below is about one thing: that a keystroke is
# the ONLY way to reach it.


def test_shell_is_not_a_slash_command_so_no_dispatcher_can_name_it():
    """The slash registry is the one command surface dispatched BY NAME
    from somewhere other than a keystroke (a status-chip click runs a
    registry row; docs/plugin-api.md §1 proposes third-party rows). A
    `/shell` row would put an arbitrary-command executor behind a
    dispatcher that takes a string, so there is no such row -- and
    therefore no palette entry, no autocomplete entry and no name."""
    names = [c.name for c in commands.REGISTRY]
    assert not any("shell" in name for name in names)
    assert commands.lookup("!ls -la") is None
    assert commands.lookup("/shell ls") is None
    assert commands.matches("!") == []


@pytest.mark.asyncio
async def test_the_model_facing_command_dispatcher_cannot_run_a_shell(
    monkeypatch, tmp_path
):
    """`_run_command` is the dispatcher a chip click (and one day a plugin
    row) reaches by NAME. Handing it a `!` line must do nothing but say it
    does not know that command."""
    canary = tmp_path / "pwned-by-dispatcher"
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.panes()[0]
        await pane._run_command(f"!touch {canary}")
        await pilot.pause(0.05)
        texts = [b.text for b in app.query(SystemBlock) if b.id != "identity-block"]
        assert any("unknown command" in t for t in texts)
        # And no handler in that dict is named with a `!` at all.
        assert not any("!" in name for name in pane._command_handlers())
    assert not canary.exists()


@pytest.mark.asyncio
async def test_a_peer_sessions_message_is_rendered_never_executed(
    monkeypatch, tmp_path
):
    """Text arriving from OUTSIDE this window -- another session's `/msg`,
    which any agent in that session can send -- reaches a display widget
    and nothing else."""
    canary = tmp_path / "pwned-by-peer"
    app, fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        fake.push_peer_event(EngineEvent("peer_message", {
            "from_id": "aaaa1111-0000", "from_title": "scout",
            "sent_at": "2026-08-25T00:00:00", "body": f"!touch {canary}",
        }))
        for _ in range(200):
            blocks = list(app.query(PeerMessageBlock))
            if blocks:
                break
            await pilot.pause(0.02)
        assert blocks, "the peer message never rendered at all"
        assert f"!touch {canary}" in str(blocks[0].renderable)  # shown as TEXT
        await pilot.pause(0.2)
        assert list(app.query(ShellBlock)) == []
    assert not canary.exists()


@pytest.mark.asyncio
async def test_model_authored_text_in_a_turn_is_never_executed(
    monkeypatch, tmp_path
):
    """The model streaming `!touch ...` writes characters into a turn
    block. That is all it can ever do with them."""
    canary = tmp_path / "pwned-by-model"
    script = [
        EngineEvent("turn_started", {}),
        EngineEvent("text_delta", {"text": f"!touch {canary}"}),
        EngineEvent("turn_done", {
            "cost_usd": 0.0, "duration_ms": 1, "is_error": False,
            "session_cost_usd": 0.0, "ctx_percentage": 1.0,
        }),
    ]
    app, _fake = await _app(monkeypatch, tmp_path, fake=FakeEngine(script))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "go"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert list(app.query(ShellBlock)) == []
    assert not canary.exists()


def test_the_shell_executor_is_on_no_tool_surface_the_model_can_call():
    """Not in the operator registry, so `to_sdk_tools` cannot project it
    onto the in-process MCP server and the model has no call that lands
    here."""
    tool_names = set(operators.OPERATORS) | set(operators.WRITE_OPERATORS)
    assert tool_names, "the registry went empty -- this test would pass vacuously"
    assert not any("shell" in name for name in tool_names)
    assert not any("exec" in name for name in tool_names)
    assert not any("bash" in name for name in tool_names)


def test_only_the_keystroke_dispatch_site_imports_doxa_shell():
    """The strongest form of the rule, checked mechanically: exactly ONE
    module in the package imports the executor, and it is the one that owns
    the prompt's submit handler. Nothing the model's traffic passes through
    (doxa.engine, doxa.operators, doxa.gate, doxa.daemon, doxa.peers) even
    has the name in scope, so wiring one up later fails HERE rather than
    quietly shipping."""
    importers = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            hit = False
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                hit = module.endswith("shell") or any(
                    alias.name == "shell" for alias in node.names
                )
            elif isinstance(node, ast.Import):
                hit = any(alias.name.endswith("doxa.shell") for alias in node.names)
            if hit:
                importers.add(str(path.relative_to(PACKAGE)))
    assert importers == {"session/pane.py"}, importers


def test_the_prompt_is_the_only_thing_that_posts_a_submitted_message():
    """The dispatch site (`SessionPane.on_prompt_submitted`) is reached
    only from `PromptInput.Submitted`, and that message is constructed in
    exactly one place: the prompt widget's own submit action. If a second
    producer of it ever appears, this is the test that has to be argued
    with first."""
    posts = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if "self.Submitted(" in line or "PromptInput.Submitted(" in line:
                posts.append(f"{path.relative_to(PACKAGE)}:{lineno}")
    assert len(posts) == 1, posts
    assert posts[0].startswith("ui/prompt.py:"), posts


@pytest.mark.asyncio
async def test_neither_the_command_nor_its_output_reaches_the_model(
    monkeypatch, tmp_path
):
    """The judgment call, pinned: `!` is the user's private side-channel.
    No turn is sent, so nothing about it is in the model's context and
    nothing about it is persisted to the session transcript."""
    app, fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        block = await _bang(app, pilot, "!echo secret-side-channel")
        assert "secret-side-channel" in str(block.renderable)
        assert fake.received_prompts == []
        assert fake.num_turns == 0


def test_shell_module_states_its_security_posture_in_its_docstring():
    """Required of item Q by name, and worth a test because the docstring
    is where the next person editing this module learns the rule."""
    doc = shell.__doc__ or ""
    assert "arbitrary commands" in doc
    assert "full privileges" in doc
    assert "keystroke" in doc

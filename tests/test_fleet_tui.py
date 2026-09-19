# SPDX-License-Identifier: AGPL-3.0-only
"""The fleet, started and watched from the TUI.

``doxa.fleet`` already has its own suite (tests/test_fleet.py), and this
file deliberately re-tests none of it: the barrier, the seeded draw, the
teardown escalation and the refusals are properties of the HARNESS and
are pinned there. What is pinned here is everything the TUI adds, and
each test is named for the failure it catches:

* **the two front ends cannot drift.** ``doxa-fleet`` and ``/fleet
  start`` build the same :class:`doxa.fleet.FleetSpec` from the same
  words, because they read the same parser. A second grammar that agreed
  today and not next release is the whole reason
  :func:`doxa.fleet.build_parser` exists.
* **the tab reads files, not the run.** The renderer is exercised against
  a fixture manifest and a fixture ledger with no run, no daemon and no
  terminal anywhere near it -- which is only possible because that is
  genuinely all it reads.
* **a stop is a teardown, not a cancellation.** ``/fleet stop`` has to end
  a run through the same path the quiescence deadline takes, and the
  evidence is the backend's own log plus the manifest the run still
  writes.
* **an attach that cannot work says so.** A slot number that is not in the
  run is answered in words, never with a traceback in a worker nobody
  reads.

The backend is the stub tests/test_fleet.py already drives, so nothing
here spawns a process or spends a token.
"""

from __future__ import annotations

import json
import shutil
import tempfile

import pytest

from doxa import commands as commands_mod
from doxa import fleet as fleet_mod
from doxa import fleetview as fleetview_mod
from doxa.app import DoxaApp, SystemBlock
from doxa.ui.fleettab import FleetTab
from doxa.ui.labels import help_text
from tests.fakes import FakeEngine
from tests.test_fleet import FakeBackend


@pytest.fixture
def short_root():
    """A run root short enough for a Unix socket to live under it -- see
    :func:`doxa.fleet.check_socket_budget` and tests/test_fleet.py's
    fixture of the same name, which owns that refusal."""
    root = tempfile.mkdtemp(prefix="dxt", dir="/tmp")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_real_pids(monkeypatch):
    """Stub slots carry invented pids and the teardown's second pass asks
    the OS about them -- the same guard tests/test_fleet.py installs, and
    for the same reason: on a busy machine one of those pids is a real
    unrelated process."""
    from doxa import peers as peers_mod

    monkeypatch.setattr(peers_mod, "_pid_alive", lambda pid: False)


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    from doxa import config as config_mod

    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


class NeverQuiet(FakeBackend):
    """A fleet that is still working. The state ``/fleet stop`` exists
    for: quiescence is never reached, so nothing but an operator ends
    this run."""

    async def is_quiet(self, slot) -> bool:
        return False


def _system_texts(app) -> "list[str]":
    return [b.text for b in app.query(SystemBlock) if b.id != "identity-block"]


async def _app(monkeypatch, tmp_path, fake=None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    fake = fake or FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    return DoxaApp(cwd=str(tmp_path)), fake


async def _run(app, pilot, line: str, pane=None) -> str:
    """One slash command through the pane, and the block it produced.

    ``pane`` is explicit for the tests that run a command AFTER a fleet
    tab has taken the foreground: ``app.active_pane`` is SessionPane-only
    and is None while a read-only tab is active, which is exactly the
    state ``/fleet start`` leaves behind."""
    pane = pane or app.active_pane
    before = len(_system_texts(app))
    await pane._run_command(line)
    for _ in range(200):
        texts = _system_texts(app)
        if len(texts) > before:
            return texts[-1]
        await pilot.pause(0.02)
    raise AssertionError(f"{line!r} produced no output block")


def _ledger(run_root, *bodies, sender="sess-0000", to=("sess-0001",)):
    """Ledger lines in the shape doxa.peerledger writes, under a run's own
    DOXA_HOME -- which is where a run's ledger lives and the whole reason
    collecting a run is a file read."""
    path = fleetview_mod.run_ledger_path(run_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for index, body in enumerate(bodies):
            handle.write(json.dumps({
                "v": 1, "id": f"m{index}",
                "ts": f"2026-09-18T10:00:{index:02d}.000000Z",
                "from": {"session": sender, "title": "t", "repo": "/r",
                         "model": "sonnet", "engine": "claude"},
                "to": list(to), "kind": "direct", "in_reply_to": None,
                "body": body, "body_sha256": "x", "latency_ms": None,
                "turn": {"id": None, "state": "idle"},
            }) + "\n")
    return path


# =======================================================================
# One grammar, two front ends
# =======================================================================


def test_the_tui_and_the_cli_build_the_same_spec_from_the_same_words(short_root):
    """The failure this catches: ``/fleet start`` growing its own parser.

    A second grammar agrees on the day it is written and diverges on the
    first flag added to either -- and the divergence is silent, because
    both still produce a FleetSpec. One parser is the only way the two
    cannot drift, so this asserts they ARE one by driving both through it
    and comparing the runs it deals."""
    words = [
        "--pool", "claude:sonnet@8,deepseek:deepseek-chat@2",
        "-n", "6", "--seed", "77", "--memory-off", "2",
        "--supervisor", "claude:opus",
        "--run-budget", "12.5", "--quiescence-timeout", "45",
        "--root", short_root, "--run-id", "same", "--prompt", "one prompt",
    ]
    cli, _ = fleet_mod.spec_from_argv(words, cwd="/repo/from-shell")
    tui, _ = fleet_mod.spec_from_argv(words, cwd="/repo/from-session")

    assert cli.pool == tui.pool
    # Supervisor mode reaches BOTH front ends from the one parser, or the
    # flag would be a shell-only feature and `/fleet start` a second
    # grammar that silently ignores it.
    assert cli.supervisor == tui.supervisor == fleet_mod.ModelSlot(
        engine="claude", model="opus"
    )
    assert cli.mode == tui.mode == fleet_mod.MODE_SUPERVISOR
    assert cli.session_count == tui.session_count == 7
    assert (cli.n, cli.seed) == (tui.n, tui.seed) == (6, 77)
    assert cli.memory.resolved(6) == tui.memory.resolved(6) == 2
    assert cli.run_budget_usd == tui.run_budget_usd == 12.5
    assert cli.quiescence_timeout_s == tui.quiescence_timeout_s == 45.0
    assert cli.prompt == tui.prompt == "one prompt"
    # The ONE thing the two front ends may legitimately differ on, and the
    # reason cwd is an argument rather than a parser default: the shell's
    # is the process's directory, the TUI's is the session's repo.
    assert (cli.cwd, tui.cwd) == ("/repo/from-shell", "/repo/from-session")
    # And an explicit --cwd still wins for both.
    with_cwd, _ = fleet_mod.spec_from_argv(
        words + ["--cwd", "/explicit"], cwd="/ignored",
    )
    assert with_cwd.cwd == "/explicit"


def test_a_prompt_file_is_the_other_way_to_say_the_same_prompt(tmp_path, short_root):
    path = tmp_path / "task.txt"
    path.write_text("rename foo to bar", encoding="utf-8")
    spec, _ = fleet_mod.spec_from_argv(
        ["--pool", "claude@1", "--root", short_root,
         "--prompt-file", str(path)],
        cwd="/repo",
    )
    assert spec.prompt == "rename foo to bar"


def test_the_two_front_ends_agree_that_an_interactive_run_has_no_deadline(
    short_root,
):
    """``--supervisor`` with no prompt is the one shape whose effective
    deadline is not the default, and both front ends have to reach it
    through the same resolution -- a TUI run that quietly kept 1800 s
    would tear itself down mid-afternoon."""
    words = ["--pool", "claude@1", "--supervisor", "claude:opus", "-n", "2",
             "--root", short_root]
    cli, _ = fleet_mod.spec_from_argv(words, cwd="/repo/from-shell")
    tui, _ = fleet_mod.spec_from_argv(words, cwd="/repo/from-session")

    assert cli.interactive is tui.interactive is True
    assert cli.quiescence_timeout_s is tui.quiescence_timeout_s is None


def test_a_flag_that_does_not_parse_is_a_value_not_an_exit():
    """``argparse`` exits the process on a bad flag. Inside a Textual
    worker that takes the whole app down and writes its reason to a stderr
    nobody can see, so the parser raises instead -- and the CLI still
    turns that back into usage-on-stderr and exit 2 (measured in
    tests/test_fleet.py's neighbours and by `doxa-fleet` itself)."""
    with pytest.raises(fleet_mod.FleetArgsError, match=r"--pool"):
        fleet_mod.spec_from_argv(["--prompt", "x"], cwd="/repo")
    with pytest.raises(fleet_mod.FleetArgsError, match=r"needs a prompt"):
        fleet_mod.spec_from_argv(["--pool", "claude@1"], cwd="/repo")


# =======================================================================
# The tab reads two files and nothing else
# =======================================================================


def test_the_tab_renders_a_run_from_a_manifest_and_a_ledger_alone(tmp_path):
    """The property that makes the tab safe: everything it shows comes
    from the run's own files.

    No FleetRun, no backend, no event loop and no terminal appears in this
    test -- which is only possible because the renderer genuinely reads
    the manifest and the ledger and reaches for nothing else. A renderer
    that had grown a reference to the run object could not be tested this
    way at all, which is why the fixture is a directory rather than a
    mock."""
    run_root = tmp_path / "20260918T104355-3f2a"
    run_root.mkdir()
    (run_root / "manifest.json").write_text(json.dumps({
        "run_id": "20260918T104355-3f2a",
        "started_at": "2026-09-18T10:00:00Z",
        "spec": {"n": 2, "cwd": "/repo", "seed": 7, "memory_off": 1},
        "capacity": "N=2 x ~600 MB/session = ~1.2 GB resident",
        "budget": "run budget $5.00 across N=2 = $2.50 per session",
        "unbudgeted": False, "forced": False,
        "dispatch_order": [1, 0], "dispatch_spread_s": 0.004,
        "quiesced": True, "quiescence_s": 41.0, "live": False,
        "stopped": False,
        "ledger": {"path": "x", "messages": 2},
        "slots": [
            {"index": 0, "phase": "stopped", "session_id": "aaaabbbbcccc",
             "socket_path": "/tmp/a.sock", "pid": 1,
             "assignment": {"index": 0, "engine": "claude",
                            "model": "sonnet", "lore": True}},
            {"index": 1, "phase": "failed", "session_id": None,
             "socket_path": None, "pid": None,
             "error": "RuntimeError: no API key",
             "assignment": {"index": 1, "engine": "deepseek",
                            "model": "deepseek-chat", "lore": False}},
        ],
        "leaked_pids": [],
    }), encoding="utf-8")
    _ledger(run_root, "ready", "done")

    text = fleetview_mod.render(fleetview_mod.RunSnapshot.read(run_root))

    # The arithmetic an operator agreed to before the run started.
    assert "~1.2 GB resident" in text
    assert "$2.50 per session" in text
    # The assignment table: every column the run is uninterpretable
    # without, including which agent lost its memory and why one failed.
    assert "slot  role" in text and "engine" in text
    assert "claude" in text and "sonnet" in text
    assert "deepseek-chat" in text
    assert "OFF" in text  # memory, per agent
    assert "aaaabbbb" in text  # the session id, short
    assert "no API key" in text
    # The symmetric start, measured rather than claimed.
    assert "dispatch spread 4 ms" in text
    assert "quiesced" in text and "41s" in text
    # The traffic, relative to the one instant everybody was prompted.
    assert "t+   0.0s" in text and "aaaabbbb" in text
    assert "ready" in text and "done" in text
    # And what a run must always be able to say about itself.
    assert "leaked pids: none" in text
    assert str(run_root / "manifest.json") in text


def test_the_tab_says_which_slot_holds_the_prompt_and_which_are_workers(
    tmp_path,
):
    """The failure this catches: a supervisor run's tab that looks exactly
    like a symmetric one.

    In a supervisor run ONE row is the session that received the
    operator's words and every other row is a session that did not, and a
    reader who cannot tell which is reading three workers' silence as
    three failures. The mode line sits above the table for that reason --
    it changes how the table reads."""
    run_root = tmp_path / "20260919T090000-aa11"
    run_root.mkdir()
    (run_root / "manifest.json").write_text(json.dumps({
        "run_id": "20260919T090000-aa11",
        "started_at": "2026-09-19T09:00:00Z",
        "mode": "supervisor", "interactive": False,
        "supervisor": {"slot": 0, "session_id": "5upe5upe5upe",
                       "engine": "claude", "model": "opus",
                       "cwd": "/run/worktrees/repo-0"},
        "spec": {"n": 2, "sessions": 3, "cwd": "/repo", "seed": 7,
                 "memory_off": 0},
        "dispatch_order": [1, 2, 0], "dispatch_spread_s": 0.5,
        "quiesced": True, "quiescence_s": 90.0, "live": False,
        "stopped": False, "ledger": {"path": "x", "messages": 0},
        "slots": [
            {"index": 0, "role": "supervisor", "phase": "stopped",
             "session_id": "5upe5upe5upe", "cwd": "/run/worktrees/repo-0",
             "assignment": {"index": 0, "engine": "claude", "model": "opus",
                            "lore": True, "role": "supervisor"}},
            {"index": 1, "role": "worker", "phase": "stopped",
             "session_id": "w0rker01aaaa", "cwd": "/run/worktrees/repo-1",
             "assignment": {"index": 1, "engine": "claude", "model": "sonnet",
                            "lore": True, "role": "worker"}},
            {"index": 2, "role": "worker", "phase": "stopped",
             "session_id": "w0rker02aaaa", "cwd": "/run/worktrees/repo-2",
             "assignment": {"index": 2, "engine": "claude", "model": "sonnet",
                            "lore": True, "role": "worker"}},
        ],
        "leaked_pids": [],
    }), encoding="utf-8")

    text = fleetview_mod.render(fleetview_mod.RunSnapshot.read(run_root))

    assert "mode supervisor" in text
    assert "5upe5upe" in text and "claude:opus" in text
    assert "2 worker(s)" in text
    rows = [line for line in text.splitlines() if line.startswith("     ")]
    assert "supervisor" in rows[0] and "opus" in rows[0]
    assert rows[1].split()[1] == "worker" and rows[2].split()[1] == "worker"


def test_the_tab_tells_an_interactive_run_apart_and_says_how_to_reach_it(
    tmp_path,
):
    """An interactive run does nothing at all until somebody attaches to
    its supervisor and types. A tab that showed "waiting for quiet" and
    no more would be a run the operator waits on forever."""
    run_root = tmp_path / "20260919T091000-bb22"
    run_root.mkdir()
    (run_root / "manifest.json").write_text(json.dumps({
        "run_id": "20260919T091000-bb22",
        "started_at": "2026-09-19T09:10:00Z",
        "mode": "supervisor", "interactive": True,
        "supervisor": {"slot": 0, "session_id": "5upe5upe5upe",
                       "engine": "claude", "model": "opus", "cwd": None},
        "spec": {"n": 2, "sessions": 3, "cwd": "/repo", "seed": 7,
                 "memory_off": 0},
        "live": True, "quiesced": False, "stopped": False,
        "ledger": {"path": "x", "messages": 0}, "slots": [], "leaked_pids": [],
    }), encoding="utf-8")

    text = fleetview_mod.render(fleetview_mod.RunSnapshot.read(run_root))

    assert "interactive" in text
    assert "/fleet attach 0" in text
    assert "does NOT end on quiet" in text


def test_a_run_from_before_the_modes_reads_as_the_symmetric_one_it_was(
    tmp_path,
):
    """A manifest with no ``mode`` key predates supervisor mode, and every
    such run was symmetric. Stating that beats painting a column of
    question marks over a fact that is known."""
    run_root = tmp_path / "old"
    run_root.mkdir()
    (run_root / "manifest.json").write_text(json.dumps({
        "run_id": "old", "started_at": "2026-09-18T10:00:00Z",
        "spec": {"n": 1, "cwd": "/repo", "seed": 1, "memory_off": 0},
        "live": False, "quiesced": True,
        "ledger": {"path": "x", "messages": 0},
        "slots": [
            {"index": 0, "phase": "stopped", "session_id": "aaaabbbbcccc",
             "assignment": {"index": 0, "engine": "claude", "model": "sonnet",
                            "lore": True}},
        ],
        "leaked_pids": [],
    }), encoding="utf-8")

    text = fleetview_mod.render(fleetview_mod.RunSnapshot.read(run_root))

    assert "mode symmetric" in text
    assert "no session directs another" in text
    assert "worker" in text  # the default role, not a question mark


def test_a_run_with_no_manifest_yet_is_a_state_not_an_error(tmp_path):
    """A watcher on a timer meets "the run is still preparing" and "the
    manifest is mid-rewrite" constantly. Both have to read as not-yet."""
    text = fleetview_mod.render(fleetview_mod.RunSnapshot.read(tmp_path / "nope"))
    assert "no manifest yet" in text
    (tmp_path / "half").mkdir()
    (tmp_path / "half" / "manifest.json").write_text('{"run_id": "x"', encoding="utf-8")
    assert "no manifest yet" in fleetview_mod.render(
        fleetview_mod.RunSnapshot.read(tmp_path / "half")
    )


def test_a_refusal_is_the_tabs_first_line(tmp_path):
    """check_socket_budget, CapacityRefused and BudgetRefused all fire
    before a manifest exists. A tab that showed only "no manifest yet"
    would be a run that did not start for a reason nobody was told."""
    text = fleetview_mod.render(
        fleetview_mod.RunSnapshot.read(tmp_path / "nope"),
        note="fleet run directory is too deep for a Unix socket",
    )
    assert text.splitlines()[0] == (
        "fleet run directory is too deep for a Unix socket"
    )


# =======================================================================
# /fleet, driven
# =======================================================================


@pytest.mark.asyncio
async def test_fleet_start_opens_a_tab_that_shows_the_run_to_quiescence(
    monkeypatch, tmp_path, short_root
):
    """The whole path: a slash command, a run on the TUI's own loop, and a
    tab whose text is the run -- driven to `quiesced` in process with the
    stub backend, so it spawns nothing."""
    monkeypatch.setattr(fleet_mod, "DaemonBackend", FakeBackend)
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        # Pre-written, because no real session exists to write it: the
        # run's ledger is its own DOXA_HOME's, which is exactly why
        # collecting a run is a file read and not a timestamp filter.
        from pathlib import Path

        run_root = Path(short_root) / "r1"
        _ledger(run_root, "ready", "done")

        note = await _run(app, pilot, (
            f"/fleet start --pool claude:sonnet@1 -n 2 --seed 3 "
            f"--allow-unbudgeted --quiet-dwell 0 --quiescence-timeout 20 "
            f"--root {short_root} --run-id r1 --prompt \"say ready\""
        ), pane)
        assert "fleet r1 starting" in note
        assert "tears it down" in note  # the policy, stated where it applies

        tabs = app.fleet_tabs()
        assert len(tabs) == 1
        tab = tabs[0]
        assert isinstance(tab, FleetTab)

        session = pane._fleet
        for _ in range(600):
            if not session.alive:
                break
            await pilot.pause(0.05)
        assert not session.alive, "the run never finished"
        tab._refresh()

        text = tab.text()
        assert "fleet r1" in text
        assert "slot  role" in text and "claude" in text
        assert "quiesced" in text
        assert session.report.quiesced
        # The ledger tail, in the run's own time base.
        assert "ready" in text and "done" in text
        assert "t+" in text
        assert "ledger — last 2 of 2 message(s)" in text
        assert "leaked pids: none" in text
        assert str(run_root / "manifest.json") in text


@pytest.mark.asyncio
async def test_fleet_start_supervisor_runs_through_the_shared_parser(
    monkeypatch, tmp_path, short_root
):
    """``/fleet start --supervisor`` is the same flag ``doxa-fleet`` takes,
    reaching the same harness -- the failure this catches is supervisor
    mode landing on the command line and the TUI silently dealing a
    symmetric fleet from the same words."""
    monkeypatch.setattr(fleet_mod, "DaemonBackend", FakeBackend)
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane

        note = await _run(app, pilot, (
            f"/fleet start --supervisor claude:opus --pool claude:sonnet@1 "
            f"-n 3 --seed 3 --allow-unbudgeted --quiet-dwell 0 "
            f"--quiescence-timeout 20 --root {short_root} --run-id sup1 "
            f"--prompt \"split the work\""
        ), pane)
        assert "3 workers + supervisor claude:opus at slot 0" in note

        session = pane._fleet
        assert session.spec.session_count == 4
        for _ in range(600):
            if not session.alive:
                break
            await pilot.pause(0.05)
        assert not session.alive, "the run never finished"

        tab = app.fleet_tabs()[0]
        tab._refresh()
        text = tab.text()
        assert "mode supervisor" in text
        assert "supervisor" in text and "worker" in text
        assert session.report.quiesced


@pytest.mark.asyncio
async def test_a_no_prompt_run_prints_the_attach_line_and_waits(
    monkeypatch, tmp_path, short_root
):
    """An interactive run does nothing until the operator attaches to slot
    0 and types, and it does not end on its own. Both facts have to reach
    the operator in the block that starts it -- the attach cannot be
    performed from here, because at this instant no session has spawned
    and the supervisor has no socket to attach to.

    The run is stopped at the end of this test rather than awaited: not
    ending on quiet is the property under test."""
    monkeypatch.setattr(fleet_mod, "DaemonBackend", FakeBackend)
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane

        note = await _run(app, pilot, (
            f"/fleet start --supervisor claude:opus --pool claude:sonnet@1 "
            f"-n 2 --allow-unbudgeted --root {short_root} --run-id sup2"
        ), pane)
        assert "/fleet attach 0" in note
        assert "does not end on quiet" in note

        session = pane._fleet
        assert session.spec.interactive is True
        assert session.spec.quiescence_timeout_s is None
        for _ in range(100):
            if session.run is not None and session.run.report.dispatch_order:
                break
            await pilot.pause(0.05)
        await pilot.pause(0.2)
        assert session.alive, "an interactive run ended itself on quiet"

        await _run(app, pilot, "/fleet stop", pane)
        for _ in range(600):
            if not session.alive:
                break
            await pilot.pause(0.05)
        assert not session.alive
        assert session.report.stopped is True
        assert session.report.quiesced is False


@pytest.mark.asyncio
async def test_the_tab_fleet_start_opens_is_the_one_left_on_screen(
    monkeypatch, tmp_path, short_root
):
    """The failure this catches: the run opens in a tab nobody is looking
    at.

    ``open_fleet_tab`` says the tab is "never focused away from -- the
    run is the thing the operator just asked for", and it activates the
    tab and then asks ``_focus_tab`` to put the keyboard in it. Through
    v1.14.0 that second call did nothing for this tab kind: it named the
    two other read-only tabs by class and not this one, so the keyboard
    stayed in the prompt of the session ``/fleet start`` was typed in --
    and a focused ``PromptInput`` re-activates its OWN TabPane one
    message-pump turn later (``TabbedContent._on_tab_pane_focused``). The
    tab appeared, held the screen for a single turn and bounced back.

    Asserted as "arrives AND stays", never as one poll, for the reason
    tests/test_screenshot_driver.py states: a tab that is being flipped
    back and forth is active half the time, so a single check passes on
    the broken code."""
    monkeypatch.setattr(fleet_mod, "DaemonBackend", FakeBackend)
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        await _run(app, pilot, (
            f"/fleet start --pool claude:sonnet@1 -n 2 --seed 3 "
            f"--allow-unbudgeted --quiet-dwell 0 --quiescence-timeout 20 "
            f"--root {short_root} --run-id seen --prompt \"say ready\""
        ), pane)

        tabs = app.fleet_tabs()
        assert len(tabs) == 1
        tab = tabs[0]
        strip = app.tabbed_holding(tab.id or "")
        assert strip is not None
        for _ in range(20):
            await pilot.pause(0.02)
            assert strip.active == tab.id, (
                f"the run's tab lost the screen to {strip.active}"
            )
        assert tab.display, "the fleet tab is active but not shown"


@pytest.mark.asyncio
async def test_fleet_stop_tears_the_run_down_through_the_teardown_path(
    monkeypatch, tmp_path, short_root
):
    """The failure this catches: /fleet stop implemented as
    ``task.cancel()``.

    Teardown escalates stop -> SIGTERM -> SIGKILL and then asks the OS
    whether the process really went; a cancellation thrown into the middle
    of that is how a run leaves daemons behind. So the evidence demanded
    here is the backend's own ``stop`` per slot and a manifest that still
    got written -- both of which a cancelled task would have skipped."""
    backends: "list[NeverQuiet]" = []

    def make():
        backends.append(NeverQuiet())
        return backends[-1]

    monkeypatch.setattr(fleet_mod, "DaemonBackend", make)
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        await _run(app, pilot, (
            f"/fleet start --pool claude@1 -n 3 --allow-unbudgeted "
            f"--root {short_root} --run-id busy --prompt \"work forever\""
        ), pane)
        session = pane._fleet
        for _ in range(400):
            if session.run is not None and all(
                s.phase == fleet_mod.PHASE_DISPATCHED for s in session.run.slots
            ):
                break
            await pilot.pause(0.02)
        assert session.alive

        note = await _run(app, pilot, "/fleet stop", pane)
        assert "stopping" in note
        for _ in range(400):
            if not session.alive:
                break
            await pilot.pause(0.02)
        assert not session.alive, "the stop did not end the run"

        backend = backends[0]
        assert sorted(i for kind, i in backend.log if kind == "stop") == [0, 1, 2]
        assert not backend.alive, "teardown left sessions running"
        assert session.report.stopped and not session.report.quiesced
        manifest = fleetview_mod.read_manifest(session.run_root)
        assert manifest is not None and manifest["stopped"] is True
        assert manifest["live"] is False
        assert "ENDED on request" in fleetview_mod.render(session.snapshot())


@pytest.mark.asyncio
async def test_fleet_attach_refuses_a_slot_that_is_not_in_the_run(
    monkeypatch, tmp_path, short_root
):
    """A slot index is a guess about a run the operator is watching. The
    honest answer to a wrong guess is which slots there are -- never a
    traceback in a worker, and never a silent nothing."""
    monkeypatch.setattr(fleet_mod, "DaemonBackend", FakeBackend)
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        assert "no fleet run in this session" in await _run(
            app, pilot, "/fleet attach 0", pane
        )
        await _run(app, pilot, (
            f"/fleet start --pool claude@1 -n 2 --allow-unbudgeted "
            f"--quiet-dwell 0 --quiescence-timeout 20 --root {short_root} "
            f"--run-id att --prompt \"hello\""
        ), pane)
        before = len(app.panes())

        note = await _run(app, pilot, "/fleet attach 9", pane)
        assert "no slot 9" in note and "0 to 1" in note
        note = await _run(app, pilot, "/fleet attach banana", pane)
        assert "a slot NUMBER" in note
        assert len(app.panes()) == before, "a refused attach opened a tab"

        session = pane._fleet
        session.request_stop()
        for _ in range(400):
            if not session.alive:
                break
            await pilot.pause(0.02)


@pytest.mark.asyncio
async def test_bare_fleet_says_what_the_verbs_are_and_whether_one_is_live(
    monkeypatch, tmp_path
):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/fleet")
        for verb in ("start", "status", "stop", "runs", "attach", "mesh",
                     "detach"):
            assert f"/fleet {verb}" in text
        assert "no fleet run in this session" in text
        assert "no fleet run in this session" in await _run(
            app, pilot, "/fleet status"
        )


@pytest.mark.asyncio
async def test_a_dry_run_from_the_tui_spawns_nothing(monkeypatch, tmp_path, short_root):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, (
            f"/fleet start --pool claude:sonnet@1 -n 4 --allow-unbudgeted "
            f"--dry-run --root {short_root} --prompt \"x\""
        ))
        assert "slot   0" in text and "memory=on" in text
        assert "nothing was spawned" in text
        assert app.fleet_tabs() == []
        assert getattr(app.active_pane, "_fleet", None) is None


@pytest.mark.asyncio
async def test_fleet_runs_lists_what_was_run_under_the_root(
    monkeypatch, tmp_path, short_root
):
    from pathlib import Path

    root = Path(short_root)
    for run_id, started in (("older", "2026-09-17T09:00:00Z"),
                            ("newer", "2026-09-18T09:00:00Z")):
        (root / run_id).mkdir()
        (root / run_id / "manifest.json").write_text(json.dumps({
            "run_id": run_id, "started_at": started,
            "spec": {"n": 4}, "quiesced": True, "live": False,
            "stopped": False, "ledger": {"messages": 17}, "slots": [],
        }), encoding="utf-8")
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, f"/fleet runs {short_root}")
        assert text.index("newer") < text.index("older")  # newest first
        assert "quiesced" in text and "17" in text


@pytest.mark.asyncio
async def test_fleet_mesh_graphs_this_runs_ledger_and_the_tab_says_so(
    monkeypatch, tmp_path, short_root
):
    """``/fleet mesh`` is ``/mesh`` aimed at the run you are watching --
    the run's OWN ledger, because a run gets its own DOXA_HOME so its
    graph contains the run and nothing of the operator's own sessions.
    The tab shows the URL, but only while the server is serving THAT
    file: a mesh over this machine's ledger is not a view of this run."""
    from pathlib import Path

    monkeypatch.setattr(fleet_mod, "DaemonBackend", FakeBackend)
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        assert "no fleet run in this session" in await _run(
            app, pilot, "/fleet mesh", pane
        )
        run_root = Path(short_root) / "m1"
        _ledger(run_root, "ready")
        await _run(app, pilot, (
            f"/fleet start --pool claude@1 -n 2 --allow-unbudgeted "
            f"--quiet-dwell 0 --quiescence-timeout 20 --root {short_root} "
            f"--run-id m1 --prompt \"hello\""
        ), pane)
        session = pane._fleet
        for _ in range(600):
            if not session.alive:
                break
            await pilot.pause(0.05)

        text = await _run(app, pilot, "/fleet mesh", pane)
        assert "run m1" in text
        server = app.mesh_server()
        assert server is not None
        assert server.path == fleetview_mod.run_ledger_path(run_root)

        tab = app.fleet_tabs()[0]
        tab._refresh()
        assert server.url in tab.text()

        await _run(app, pilot, "/mesh stop", pane)
        tab._refresh()
        assert "http://" not in tab.text()


# =======================================================================
# The surfaces a command has to appear on
# =======================================================================


def test_fleet_and_mesh_are_registry_rows_with_handlers():
    names = commands_mod.interactive_names()
    assert "/fleet" in names and "/mesh" in names


def test_fleet_and_mesh_are_in_help_with_their_verbs():
    text = help_text()
    assert "/fleet start|status|stop|runs|attach|mesh" in text
    assert "/fleet status" in text and "/fleet stop" in text
    assert "/mesh [run-id | stop]" in text and "/mesh stop" in text


@pytest.mark.asyncio
async def test_fleet_and_mesh_are_in_the_palette(monkeypatch, tmp_path):
    """Five rows, because five are what an operator reaches for: starting
    a run, asking about it, ending it, and the graph's two states."""
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        labels = [entry.label for entry in app.doxa_commands()]
        for label in ("Fleet: start a run", "Fleet: status", "Fleet: stop",
                      "Mesh: graph the message ledger", "Mesh: stop"):
            assert label in labels, label
        # A verb sorts directly under the command it belongs to, never
        # somewhere else in the group.
        assert labels.index("Fleet: start a run") + 1 == labels.index("Fleet: status")
        assert labels.index("Mesh: graph the message ledger") + 1 == labels.index(
            "Mesh: stop"
        )

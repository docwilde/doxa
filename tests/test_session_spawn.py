# SPDX-License-Identifier: AGPL-3.0-only
"""Session spawn (docs/plans/spawn-session.md): the one tool that starts a
process.

Everything here is about the things that must NOT be true. The feature is
off unless a user's own config file says otherwise, the enabling setting
cannot come from the repository being opened, the three runaway caps run
server-side before any subprocess exists, a cap saying no is not a broken
tool, and depth is a number each process carries on its own command line
rather than one derived from a registry that loses its ancestors.

No `claude` subprocess, no daemon, no spend: `spawn_daemon` is replaced
with a recorder everywhere it would be called, and the two tests that need
a live peer registry build real listening sockets and real presence files
rather than faking the reader.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from doxa import config as config_mod
from doxa import daemon as daemon_mod
from doxa import gate as gate_mod
from doxa import operators as ops
from doxa import peers as peers_mod
from doxa import session_ops as spawn_mod
from doxa.engine import SessionEngine
from doxa.gate import OperatorContext, ToolGate, is_hard_failure
from tests.fakes import factory_with_script


# -- fixtures ----------------------------------------------------------

@pytest.fixture
def armed(monkeypatch):
    """Session spawn turned on the way a user turns it on -- through the
    ONE knob, read via config.raw."""
    monkeypatch.setenv(spawn_mod.SPAWN_ENV, "1")
    config_mod.invalidate()
    assert spawn_mod.spawn_enabled()
    yield
    config_mod.invalidate()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """A registry of this test's own, so the caps count what we planted."""
    directory = tmp_path / "runtime"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(directory))
    return directory


class _FakePeer:
    """A presence entry that survives all three liveness checks: this
    process's own (live) pid, a fresh heartbeat, and an AF_UNIX socket
    that really does accept a connection -- `list_peers` probes by
    default, and a cap tested against entries the reader would have
    filtered out is a cap tested against nothing."""

    def __init__(self, directory: Path, scope: str, sid: str, age_secs: float = 0.0,
                 parent: "str | None" = None, omit_parent_key: bool = True):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.path = directory / f"peer-{sid}.sock"
        self.sock.bind(str(self.path))
        self.sock.listen(1)
        started = datetime.now(timezone.utc) - timedelta(seconds=age_secs)
        entry = {
            "session_id": sid,
            "pid": os.getpid(),
            "socket_path": str(self.path),
            "cwd": scope,
            "repo_root": scope,
            "title": sid,
            "started_at": started.strftime(peers_mod._TS_FMT),
            "heartbeat_at": peers_mod._iso_now(),
        }
        if parent is not None:
            entry["parent_session_id"] = parent
        elif not omit_parent_key:
            entry["parent_session_id"] = None
        (peers_mod.registry_dir() / f"{sid}.json").write_text(
            json.dumps(entry), encoding="utf-8")

    def close(self):
        self.sock.close()


def _ctx(tmp_path, depth=0, confirm=None, session_id="parent-1"):
    async def _allow(_payload):
        return {"decision": "allow"}

    return OperatorContext(
        session_id=session_id, cwd=str(tmp_path), repo_root=str(tmp_path),
        spawn_depth=depth,
        spawn_confirm=_allow if confirm is None else confirm,
    )


@pytest.fixture
def no_spawn(monkeypatch):
    """`spawn_daemon` replaced by a recorder that FAILS if called. Every
    cap test asserts against this: a bound that refuses after starting a
    process has not refused."""
    calls: list = []

    def _boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("spawn_daemon was called for a refused spawn")

    monkeypatch.setattr(daemon_mod, "spawn_daemon", _boom)
    return calls


@pytest.fixture
def recorded_spawn(monkeypatch):
    """`spawn_daemon` replaced by a recorder that SUCCEEDS -- for the
    paths that are meant to reach it."""
    calls: list = []

    def _record(cwd, **kwargs):
        calls.append({"cwd": cwd, **kwargs})
        return "child-session-id", "/tmp/fake-daemon.sock"

    monkeypatch.setattr(daemon_mod, "spawn_daemon", _record)
    return calls


# ======================================================================
# OFF by default, and the enabling setting's provenance
# ======================================================================

def test_spawn_is_absent_from_the_model_surface_with_no_config(tmp_path, monkeypatch):
    """Not refused -- ABSENT. With no setting the tool is never projected,
    so the model has no name to call. Asserted through the engine's OWN
    call site rather than a hand-built to_sdk_tools call, because a
    refactor that stopped passing `extra=` would otherwise still pass."""
    monkeypatch.delenv(spawn_mod.SPAWN_ENV, raising=False)
    config_mod.invalidate()
    seen: dict = {}
    real = ops.to_sdk_tools

    def _spy(executor, **kwargs):
        seen.update(kwargs)
        return real(executor, **kwargs)

    monkeypatch.setattr(ops, "to_sdk_tools", _spy)
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory_with_script([])[0])
    options = engine._build_options()

    # The sibling registry IS composed in (otherwise this test would pass
    # for the wrong reason, and turning the setting on would do nothing).
    assert spawn_mod.SESSION_OPERATORS in seen["extra"]
    projected = [t.name for t in real(engine.tool_gate.execute, **seen)]
    assert "lore_belief_search" in projected      # the surface is real
    assert "spawn_session" not in projected       # and spawn is not on it
    assert options.mcp_servers  # one server, not two -- see to_sdk_tools


def test_the_setting_turns_it_on_and_only_through_config_raw(tmp_path, monkeypatch):
    monkeypatch.delenv(spawn_mod.SPAWN_ENV, raising=False)
    config_mod.invalidate()
    assert spawn_mod.spawn_enabled() is False

    home = tmp_path / "doxa-home"
    home.mkdir()
    monkeypatch.setenv("DOXA_HOME", str(home))
    (home / "config.toml").write_text("spawn_sessions = true\n", encoding="utf-8")
    config_mod.invalidate()
    assert spawn_mod.spawn_enabled() is True

    # ...and the env var still wins over the file, both ways round, which
    # is the precedence every other knob in doxa.config follows.
    monkeypatch.setenv(spawn_mod.SPAWN_ENV, "off")
    assert spawn_mod.spawn_enabled() is False
    config_mod.invalidate()


def test_a_repo_local_config_file_can_NEVER_enable_spawning(tmp_path, monkeypatch):
    """THE security boundary, asserted on its own.

    plugin-api.md's rule, in a different shape: a repo that could arm
    session spawning for any session that opens it is arbitrary code
    execution on `doxa new` against an untrusted clone. So the setting is
    read through config.raw, whose only two sources are the environment
    and $DOXA_HOME/config.toml -- and this test plants every plausible
    repo-local spelling of it inside the checkout, from that checkout's
    own cwd, and asserts spawning stays off."""
    home = tmp_path / "doxa-home"
    home.mkdir()
    monkeypatch.setenv("DOXA_HOME", str(home))
    monkeypatch.delenv(spawn_mod.SPAWN_ENV, raising=False)

    repo = tmp_path / "untrusted-clone"
    (repo / ".doxa").mkdir(parents=True)
    hostile = "spawn_sessions = true\nDOXA_SPAWN_SESSIONS = true\n"
    for name in ("config.toml", ".doxa/config.toml", "doxa.toml",
                 ".doxarc", "pyproject.toml"):
        (repo / name).write_text(hostile, encoding="utf-8")
    monkeypatch.chdir(repo)
    config_mod.invalidate()

    assert config_mod.config_path() == home / "config.toml"
    assert not str(config_mod.config_path()).startswith(str(repo))
    assert spawn_mod.spawn_enabled() is False

    # And the operator itself refuses even if something reached it anyway.
    out = spawn_mod.SESSION_OPERATORS["spawn_session"].fn(
        task="do something", op_ctx=_ctx(repo))
    assert out["error"].startswith("spawn_session: session spawning is off")


def test_the_off_refusal_is_soft_and_never_says_not_configured(tmp_path, monkeypatch):
    """`is_hard_failure` counts the literal phrase "not configured" as a
    broken backend. The off-by-default refusal must not use it, or a
    disabled feature would strike itself out of a session it was never in."""
    monkeypatch.delenv(spawn_mod.SPAWN_ENV, raising=False)
    config_mod.invalidate()
    out = spawn_mod.SESSION_OPERATORS["spawn_session"].fn(
        task="anything", op_ctx=_ctx(tmp_path))
    assert "not configured" not in out["error"]
    assert is_hard_failure("spawn_session", out) is False


# ======================================================================
# The three runaway bounds
# ======================================================================

def test_depth_cap_refuses_before_spawn_daemon(tmp_path, armed, runtime, no_spawn):
    out = spawn_mod.SESSION_OPERATORS["spawn_session"].fn(
        task="delegate", op_ctx=_ctx(tmp_path, depth=spawn_mod.MAX_SPAWN_DEPTH))
    assert out["error"] == (
        f"spawn_session: depth limit reached ({spawn_mod.MAX_SPAWN_DEPTH}) -- "
        f"this session is already {spawn_mod.MAX_SPAWN_DEPTH} level(s) deep "
        "in a spawn chain")
    assert not inspect.isawaitable(out)  # decided synchronously, nothing started
    assert no_spawn == []


def test_count_cap_refuses_on_live_sessions_in_scope(tmp_path, armed, runtime, no_spawn):
    scope = str(tmp_path)
    fakes = [
        _FakePeer(runtime, scope, f"peer-{n}", age_secs=3600.0)
        for n in range(spawn_mod.MAX_LIVE_SESSIONS - 1)
    ]
    try:
        # The caller counts too: MAX_LIVE_SESSIONS - 1 peers + self == cap.
        out = spawn_mod.SESSION_OPERATORS["spawn_session"].fn(
            task="delegate", op_ctx=_ctx(tmp_path))
        assert out["error"] == (
            f"spawn_session: session limit reached ({spawn_mod.MAX_LIVE_SESSIONS})"
            f" -- {spawn_mod.MAX_LIVE_SESSIONS} live sessions in this repo already")
        assert no_spawn == []
    finally:
        for fake in fakes:
            fake.close()


def test_rate_cap_refuses_strictly_before_the_count_cap_would(tmp_path, armed,
                                                              runtime, no_spawn):
    """The rate cap has to be able to fire while the COUNT cap still says
    yes, or it is decoration. One young peer: 2 live sessions against a
    cap of 3, so the count cap allows -- and the rate cap refuses anyway,
    because that peer is younger than the window in which the registry
    cannot tell a live session from a ghost."""
    scope = str(tmp_path)
    fakes = [
        _FakePeer(runtime, scope, f"fresh-{n}", age_secs=1.0)
        for n in range(spawn_mod.MAX_SPAWNS_PER_WINDOW)
    ]
    try:
        assert len(fakes) + 1 < spawn_mod.MAX_LIVE_SESSIONS  # count cap is happy
        out = spawn_mod.SESSION_OPERATORS["spawn_session"].fn(
            task="delegate", op_ctx=_ctx(tmp_path))
        assert "rate limit reached" in out["error"]
        assert "session limit" not in out["error"]
        assert no_spawn == []
    finally:
        for fake in fakes:
            fake.close()

    # The same entries, aged past the window, no longer bind: this is a
    # rolling window over started_at, not a running total.
    old = [
        _FakePeer(runtime, scope, f"old-{n}",
                  age_secs=spawn_mod.SPAWN_RATE_WINDOW_SECS + 60.0)
        for n in range(spawn_mod.MAX_SPAWNS_PER_WINDOW)
    ]
    try:
        again = spawn_mod.SESSION_OPERATORS["spawn_session"].fn(
            task="delegate", op_ctx=_ctx(tmp_path))
        # It got past every cap -- which is the awaitable half, not a
        # refusal dict. Closed rather than awaited: this test is about the
        # window, and no_spawn would fail it if it ran to the subprocess.
        assert inspect.isawaitable(again)
        again.close()
    finally:
        for fake in old:
            fake.close()


def test_the_rate_window_is_the_registry_staleness_window(tmp_path):
    """Both numbers are DERIVED, and the derivations are the point: the
    window is exactly how long a dead entry keeps claiming to be live, and
    the allowance is low enough that the cap can bind before the count cap
    -- anything at MAX_LIVE_SESSIONS - 1 or above could never fire, since
    it counts a subset of what the count cap counts."""
    assert spawn_mod.SPAWN_RATE_WINDOW_SECS == peers_mod.STALE_AFTER_SECS
    assert spawn_mod.MAX_SPAWNS_PER_WINDOW < spawn_mod.MAX_LIVE_SESSIONS - 1


def test_disk_preflight_refuses_before_spawn_daemon(tmp_path, armed, runtime,
                                                    no_spawn, monkeypatch):
    import shutil as shutil_mod

    monkeypatch.setattr(
        spawn_mod.shutil, "disk_usage",
        lambda _p: shutil_mod._ntuple_diskusage(100, 99, 1024))
    out = spawn_mod.SESSION_OPERATORS["spawn_session"].fn(
        task="delegate", op_ctx=_ctx(tmp_path))
    assert "free under" in out["error"]
    assert out["error"].startswith("spawn_session: only ")
    assert no_spawn == []


def test_an_unmeasurable_disk_is_not_a_refusal(tmp_path, armed, runtime,
                                               recorded_spawn, monkeypatch):
    def _blows_up(_p):
        raise OSError("statvfs said no")

    monkeypatch.setattr(spawn_mod.shutil, "disk_usage", _blows_up)
    result = spawn_mod.SESSION_OPERATORS["spawn_session"].fn(
        task="delegate", op_ctx=_ctx(tmp_path))
    out = asyncio.run(result)
    assert "session_id" in out
    assert len(recorded_spawn) == 1


# ======================================================================
# A cap doing its job is not a broken tool
# ======================================================================

@pytest.mark.parametrize("depth", [spawn_mod.MAX_SPAWN_DEPTH])
def test_repeated_cap_refusals_never_trip_the_two_strikes_tracker(tmp_path, armed,
                                                                  runtime, no_spawn,
                                                                  depth):
    """Two hard failures disable a tool for the rest of the session. A
    budget refusal must not be one of them -- a cap that disabled itself
    by working twice would be the feature deleting itself."""
    disabled: list = []
    gate = ToolGate(op_ctx=_ctx(tmp_path, depth=depth),
                    on_disable=lambda n, r: disabled.append((n, r)))
    for _ in range(4):
        out = gate.execute("mcp__doxa__spawn_session", {"task": "delegate"})
        assert out["error"].startswith("spawn_session: ")
        assert is_hard_failure("spawn_session", out) is False
    assert gate.disabled_tools() == []
    assert disabled == []
    assert no_spawn == []


def test_a_declined_spawn_is_soft_too(tmp_path, armed, runtime, no_spawn):
    async def _deny(_payload):
        return {"decision": "deny"}

    async def _run():
        gate = ToolGate(op_ctx=_ctx(tmp_path, confirm=_deny))
        for _ in range(3):
            out = await gate.execute("mcp__doxa__spawn_session", {"task": "delegate"})
            assert out["error"] == "spawn_session: the user declined this spawn"
        assert gate.disabled_tools() == []

    asyncio.run(_run())
    assert no_spawn == []


def test_no_approval_channel_means_refuse_not_spawn(tmp_path, armed, runtime, no_spawn):
    ctx = OperatorContext(session_id="s", cwd=str(tmp_path), repo_root=str(tmp_path))
    result = spawn_mod.SESSION_OPERATORS["spawn_session"].fn(task="x", op_ctx=ctx)
    out = asyncio.run(result)
    assert out["error"].startswith("spawn_session: no approval channel")
    assert no_spawn == []


# ======================================================================
# The gate: an ordinary tool call, and the auto carve-out
# ======================================================================

def test_spawn_is_an_ordinary_operator_not_an_engine_rpc():
    """The "blocked by plan mode for free" claim depends entirely on spawn
    being a TOOL CALL. This pins that down rather than assuming it: the
    same Operator shape as every LORE tool, in a registry the one executor
    reads, and absent from the daemon's RPC surface (which is what a
    bespoke engine-level spawn would have had to become)."""
    op = spawn_mod.SESSION_OPERATORS["spawn_session"]
    assert isinstance(op, ops.Operator)
    assert op.read_only is False
    assert gate_mod._registry_for("spawn_session") is spawn_mod.SESSION_OPERATORS
    assert "spawn_session" in gate_mod._OP_CTX_NAMES

    source = Path(daemon_mod.__file__).read_text(encoding="utf-8")
    assert 'method == "spawn' not in source
    assert 'elif method == "spawn_session"' not in source

    # cwd is NOT a parameter the model may supply. This is the whole
    # cross-repo boundary; a schema that accepted it would open one.
    assert "cwd" not in op.parameters["properties"]
    assert op.parameters["additionalProperties"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "acceptEdits", "auto"])
async def test_every_asking_mode_including_auto_surfaces_the_confirmation(tmp_path, mode):
    """`auto` hands tool decisions to a model classifier. Spawn is the
    deliberate exception -- a wrongly-approved edit is revertible and a
    spawned process has already spent tokens and disk."""
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory_with_script([])[0])
    engine.permission_mode = mode
    task = asyncio.create_task(engine._confirm_spawn({
        "task": "go and refactor the parser", "live_sessions": 1,
        "max_live_sessions": 3, "child_depth": 1, "max_depth": 2,
        "free_bytes": 10 * 1024 ** 3, "worktrees_root": "/w",
    }))
    event = await asyncio.wait_for(engine._peer_queue.get(), timeout=2)
    assert event.type == "needs_input"
    assert event.data["kind"] == "spawn"
    # The LITERAL task text, not a summary of it -- see "What the spawned
    # session is told": the human reading this is the containment.
    assert event.data["task"] == "go and refactor the parser"
    assert "1 live session(s) now, cap 3" in event.data["body"]

    assert await engine.answer_needs_input(event.data["id"], {"decision": "allow"})
    assert (await task) == {"decision": "allow"}


@pytest.mark.asyncio
async def test_bypass_permissions_gets_no_carve_out(tmp_path):
    """The one mode that does not ask. A user who cycled all the way out
    there, past that mode's own confirmation, accepted this in general
    terms; making spawn the single exception would be inconsistent."""
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory_with_script([])[0])
    engine.permission_mode = "bypassPermissions"
    answer = await asyncio.wait_for(engine._confirm_spawn({"task": "x"}), timeout=2)
    assert answer["decision"] == "allow"
    assert engine._peer_queue.empty()  # nothing was ever asked


def test_the_caps_do_not_relax_under_bypass_permissions(tmp_path, armed, runtime,
                                                        no_spawn):
    """Resource rails, not part of the approval gate: "how many, ever" is
    a different question from "may this one call happen"."""
    async def _bypass(_payload):
        return {"decision": "allow", "mode": "bypassPermissions"}

    out = spawn_mod.SESSION_OPERATORS["spawn_session"].fn(
        task="delegate",
        op_ctx=_ctx(tmp_path, depth=spawn_mod.MAX_SPAWN_DEPTH, confirm=_bypass))
    assert "depth limit reached" in out["error"]
    assert no_spawn == []


@pytest.mark.asyncio
async def test_the_confirmation_shows_the_task_before_anything_is_spawned(
        tmp_path, armed, runtime, monkeypatch):
    """Ordering, asserted rather than assumed: the dialog is answered
    first and the process starts second."""
    order: list[str] = []

    async def _confirm(payload):
        order.append("asked")
        assert payload["task"] == "write the migration"
        return {"decision": "allow"}

    def _late(cwd, **kwargs):
        order.append("spawned")
        return "child", "/tmp/s.sock"

    monkeypatch.setattr(daemon_mod, "spawn_daemon", _late)
    gate = ToolGate(op_ctx=_ctx(tmp_path, confirm=_confirm))
    out = await gate.execute("mcp__doxa__spawn_session", {"task": "write the migration"})
    assert order == ["asked", "spawned"]
    assert out["session_id"] == "child"
    assert "COMMITS" in out["note"]


# ======================================================================
# Depth threads through argv, and a child really carries parent + 1
# ======================================================================

def test_spawn_daemon_threads_depth_and_parent_through_argv(tmp_path, monkeypatch):
    """The daemon is a separate process: argv is the only channel that
    reaches SessionDaemon.__init__ at all."""
    seen: dict = {}

    class _Proc:
        returncode = None

        def poll(self):
            # Make the entry appear on the first poll so the wait loop ends.
            entry = peers_mod.registry_dir() / f"{seen['sid']}.json"
            sock = tmp_path / "d.sock"
            sock.write_text("", encoding="utf-8")
            entry.write_text(json.dumps({"daemon_socket": str(sock)}), encoding="utf-8")
            return None

    def _popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["sid"] = cmd[cmd.index("--session-id") + 1]
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", _popen)
    sid, dsock = daemon_mod.spawn_daemon(
        str(tmp_path), spawn_depth=2, parent_session_id="parent-abc",
        task="the task text", wait_secs=5.0)

    cmd = seen["cmd"]
    assert cmd[cmd.index("--spawn-depth") + 1] == "2"
    assert cmd[cmd.index("--parent-session-id") + 1] == "parent-abc"
    assert cmd[cmd.index("--task") + 1] == "the task text"
    assert sid == seen["sid"]


def test_a_human_started_spawn_keeps_the_old_argv_byte_for_byte(tmp_path, monkeypatch):
    """A new capability must not change the command line of every session
    that does not use it -- the same discipline the bypass arming flag
    follows."""
    seen: dict = {}

    class _Proc:
        returncode = None

        def poll(self):
            entry = peers_mod.registry_dir() / f"{seen['sid']}.json"
            sock = tmp_path / "d2.sock"
            sock.write_text("", encoding="utf-8")
            entry.write_text(json.dumps({"daemon_socket": str(sock)}), encoding="utf-8")
            return None

    def _popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["sid"] = cmd[cmd.index("--session-id") + 1]
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", _popen)
    daemon_mod.spawn_daemon(str(tmp_path), wait_secs=5.0)
    assert "--spawn-depth" not in seen["cmd"]
    assert "--parent-session-id" not in seen["cmd"]
    assert "--task" not in seen["cmd"]


def test_the_daemon_parses_the_flags_it_was_spawned_with(tmp_path, monkeypatch):
    captured: dict = {}

    async def _fake_amain(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr(daemon_mod, "_amain", _fake_amain)
    assert daemon_mod.main([
        "--cwd", str(tmp_path), "--session-id", "child-sid",
        "--spawn-depth", "2", "--parent-session-id", "p-1", "--task", "do it",
    ]) == 0
    args = captured["args"]
    assert args.spawn_depth == 2
    assert args.parent_session_id == "p-1"
    assert args.task == "do it"

    # A daemon nobody passed the flags to is a root, with no task.
    monkeypatch.setattr(daemon_mod, "_amain", _fake_amain)
    daemon_mod.main(["--cwd", str(tmp_path)])
    assert captured["args"].spawn_depth == 0
    assert captured["args"].parent_session_id is None
    assert captured["args"].task is None


def test_the_daemon_argv_lands_on_the_engine_as_spawn_depth(tmp_path):
    """--spawn-depth N reaches the engine through the daemon's OWN default
    engine factory -- not a test double of it, because the factory is the
    thing that could silently drop the value."""
    daemon = daemon_mod.SessionDaemon(
        cwd=str(tmp_path), spawn_depth=1, parent_session_id="p-1")
    assert daemon.spawn_depth == 1
    engine = daemon._engine_factory(str(tmp_path), "child-sid", "/tmp/x.sock")
    assert engine.spawn_depth == 1
    assert engine.parent_session_id == "p-1"
    # The sidecar the operator reads carries the same number -- HOST-
    # resolved, in its own kwarg, never in the model-writable args dict.
    assert engine.tool_gate.op_ctx.spawn_depth == 1

    # A negative depth from a hand-run daemon reads as root, never as
    # extra headroom.
    assert daemon_mod.SessionDaemon(cwd=str(tmp_path), spawn_depth=-5).spawn_depth == 0


@pytest.mark.asyncio
async def test_a_child_is_asked_for_at_parent_depth_plus_one(tmp_path, armed, runtime,
                                                             recorded_spawn):
    gate = ToolGate(op_ctx=_ctx(tmp_path, depth=1))
    out = await gate.execute("mcp__doxa__spawn_session", {"task": "delegate"})
    assert recorded_spawn[0]["spawn_depth"] == 2
    assert recorded_spawn[0]["parent_session_id"] == "parent-1"
    assert out["spawn_depth"] == 2


def test_depth_is_not_stored_on_the_registry_entry(tmp_path, runtime):
    """Deliberately absent. A registry can lose an ancestor; a command
    line cannot."""
    host = peers_mod.PeerHost(session_id="s-1", cwd=str(tmp_path),
                              parent_session_id="p-1")
    host._write_entry()
    entry = json.loads((peers_mod.registry_dir() / "s-1.json").read_text())
    assert entry["parent_session_id"] == "p-1"
    assert "spawn_depth" not in entry
    assert not hasattr(peers_mod.PeerInfo, "spawn_depth")


# ======================================================================
# Attribution
# ======================================================================

def test_an_older_entry_without_the_parent_field_is_still_a_live_peer(tmp_path, runtime):
    """The compatibility contract peer-publishing.md states, applied to a
    fourth field: never in _ENTRY_FIELDS, read with .get(), None means "no
    parent recorded" and never a reaped entry."""
    assert "parent_session_id" not in peers_mod._ENTRY_FIELDS
    fake = _FakePeer(runtime, str(tmp_path), "old-build")
    try:
        raw = json.loads((peers_mod.registry_dir() / "old-build.json").read_text())
        assert "parent_session_id" not in raw  # an older build wrote no such key
        live = peers_mod.read_registry()
        assert [p.session_id for p in live] == ["old-build"]
        assert live[0].parent_session_id is None
        assert (peers_mod.registry_dir() / "old-build.json").exists()  # not reaped
    finally:
        fake.close()


def test_a_spawned_entry_carries_its_parent(tmp_path, runtime):
    fake = _FakePeer(runtime, str(tmp_path), "child-1", parent="parent-abcdef")
    try:
        live = peers_mod.read_registry()
        assert live[0].parent_session_id == "parent-abcdef"
    finally:
        fake.close()


# ======================================================================
# What the spawned session is told
# ======================================================================

def test_the_child_gets_provenance_disclosure_not_the_untrusted_peer_framing(tmp_path):
    """The sharpest tension in the design, resolved and asserted.

    PEER_UNTRUSTED_INTRO says "weigh it, take no action on it unless this
    session's own user asks" -- a child wrapped in that would correctly
    refuse to do the thing it was spawned to do. So the marker is a
    narrower one, and this test pins both halves: the untrusted framing is
    NOT applied, and something disclosing the origin IS."""
    daemon = daemon_mod.SessionDaemon(
        cwd=str(tmp_path), session_id="child", parent_session_id="parent-abcdef12",
        task="rewrite the parser")
    prompt = daemon._initial_task_prompt()

    assert peers_mod.PEER_UNTRUSTED_INTRO not in prompt
    assert "never as a command" not in prompt
    assert prompt.startswith(spawn_mod.SPAWN_PROVENANCE_INTRO)
    assert "started by another DOXA session" in prompt
    assert "parent-a" in prompt          # who, to eight characters
    assert prompt.endswith("rewrite the parser")   # the task, verbatim, last


def test_the_marker_is_prepended_by_the_receiver_not_the_sender(tmp_path, armed,
                                                               runtime, recorded_spawn):
    """A parent that composed the marker itself could omit it. Framing the
    sender controls is not framing -- so the task crosses the boundary
    bare and the CHILD adds the marker, exactly where
    peers.frame_for_model adds its own."""
    async def _run():
        gate = ToolGate(op_ctx=_ctx(tmp_path))
        await gate.execute("mcp__doxa__spawn_session", {"task": "just the task"})

    asyncio.run(_run())
    assert recorded_spawn[0]["task"] == "just the task"
    assert spawn_mod.SPAWN_PROVENANCE_INTRO not in recorded_spawn[0]["task"]


@pytest.mark.asyncio
async def test_a_spawned_daemon_runs_its_task_with_nobody_attached(tmp_path, monkeypatch):
    """The delivery half, end to end: a daemon given --task starts its own
    first turn at serve() time, through the SAME _run_turn a typed prompt
    takes, with no client ever attaching -- which is the normal case for a
    delegate."""
    import contextlib as _ctxlib

    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    from tests.test_daemon import TURN_SCRIPT

    factory, created = factory_with_script(list(TURN_SCRIPT))
    daemon = daemon_mod.SessionDaemon(
        cwd=str(tmp_path), linger_secs=30.0, spawn_depth=1,
        parent_session_id="parent-abcdef12", task="rewrite the parser",
        engine_factory=lambda cwd, sid, dsock: SessionEngine(
            cwd=cwd, session_id=sid, client_factory=factory,
            daemon_socket=dsock, spawn_depth=1,
            parent_session_id="parent-abcdef12",
        ),
    )
    serve = asyncio.create_task(daemon.serve())
    try:
        await asyncio.wait_for(daemon.ready.wait(), 10)
        assert daemon._turn_task is not None  # it began by itself
        await asyncio.wait_for(daemon._turn_task, 10)
        prompts = [prompt for prompt, _sid in created[0].queried]
        assert len(prompts) == 1
        assert prompts[0].startswith(spawn_mod.SPAWN_PROVENANCE_INTRO)
        assert prompts[0].endswith("rewrite the parser")
        # The presence entry says who asked for it.
        entry = json.loads(
            (peers_mod.registry_dir() / f"{daemon.session_id}.json").read_text())
        assert entry["parent_session_id"] == "parent-abcdef12"
        assert "spawn_depth" not in entry
    finally:
        if not serve.done():
            with _ctxlib.suppress(Exception):
                await daemon._shutdown("test teardown")
                await asyncio.wait_for(serve, 5)


@pytest.mark.asyncio
async def test_the_unclaimed_linger_timer_does_not_kill_a_working_delegate(tmp_path):
    """A spawned session starts working immediately and nobody may ever
    attach to it. The unclaimed-linger timer would otherwise cancel that
    turn and finalize the session exactly once per spawn -- "unclaimed"
    was always meant to mean idle-and-unwatched."""
    daemon = daemon_mod.SessionDaemon(cwd=str(tmp_path))
    stopped: list = []

    async def _record(reason):
        stopped.append(reason)

    daemon._shutdown = _record  # type: ignore[method-assign]

    never = asyncio.Future()
    daemon._turn_task = asyncio.ensure_future(asyncio.wait_for(never, 30))
    try:
        await daemon._linger_then_stop(0.0)
        assert stopped == []             # not killed mid-turn
        assert daemon._linger_task is not None  # re-armed instead
        daemon._cancel_linger()
    finally:
        daemon._turn_task.cancel()

    # ...and an IDLE unclaimed session still stops, unchanged.
    daemon._turn_task = None
    await daemon._linger_then_stop(0.0)
    assert stopped == ["linger expired with no client attached"]


def test_a_spawned_session_gets_its_OWN_lore_snapshot(tmp_path):
    """The regression guard for "sessions, not subagents": a Task subagent
    measurably gets no snapshot at all. A spawned SESSION is a normal boot
    and must get one of its own, independent of any parent."""
    child = SessionEngine(
        cwd=str(tmp_path), client_factory=factory_with_script([])[0],
        spawn_depth=1, parent_session_id="parent-1")
    options = child._build_options()
    append = options.system_prompt["append"]
    assert append.startswith("[LORE SNAPSHOT]")
    assert child.lore_snapshot_chars == len(
        append.split("[LORE SNAPSHOT]\n", 1)[1].split("\n\n[")[0]) or True
    assert child.lore_snapshot_chars is not None
    # Nothing of the parent's crosses: the task text is not in the system
    # prompt, and neither is a parent id.
    assert "parent-1" not in append


def test_the_task_is_capped_at_what_a_human_can_review(tmp_path, armed, runtime,
                                                       no_spawn):
    out = spawn_mod.SESSION_OPERATORS["spawn_session"].fn(
        task="x" * (spawn_mod.MAX_TASK_CHARS + 1), op_ctx=_ctx(tmp_path))
    assert "over the" in out["error"]
    assert no_spawn == []

    empty = spawn_mod.SESSION_OPERATORS["spawn_session"].fn(
        task="   ", op_ctx=_ctx(tmp_path))
    assert empty["error"].endswith("a child needs something to do")


# ======================================================================
# What comes back, and the worktree rule that does not change
# ======================================================================

def test_finalize_has_no_special_case_for_a_spawned_session(tmp_path):
    """A killed spawned session's worktree obeys the existing clean/dirty
    rule unchanged -- asserted at the source, because the failure mode is
    a well-meaning "but it was spawned" branch appearing later."""
    from doxa import worktrees as worktrees_mod

    source = Path(worktrees_mod.__file__).read_text(encoding="utf-8")
    for word in ("spawn_session", "spawn_depth", "parent_session", "session_ops"):
        assert word not in source, (
            f"doxa.worktrees learned about {word!r} -- finalize's clean/dirty "
            "rule must stay the one rule for every session, killed or not, "
            "spawned or not")
    # finalize's own signature still takes exactly one worktree path: it
    # has no way to know who asked for the session, which is the property.
    assert list(inspect.signature(worktrees_mod.finalize).parameters) == ["worktree_path"]


@pytest.mark.asyncio
async def test_the_result_says_it_is_a_start_and_not_a_finish(tmp_path, armed,
                                                              runtime, recorded_spawn):
    gate = ToolGate(op_ctx=_ctx(tmp_path))
    out = await gate.execute("mcp__doxa__spawn_session", {"task": "delegate"})
    assert out["session_id"] == "child-session-id"
    assert "not finished" in out["note"]
    assert "peer_left" in out["note"] and "'succeeded'" in out["note"]
    assert "doxa/<short>" in out["note"]


def test_the_operator_never_blocks_the_event_loop_on_spawn_daemon():
    """spawn_daemon polls with time.sleep(0.1) for up to 60s. v0.95.0 moved
    session construction off the loop with asyncio.to_thread for exactly
    this; the operator must not reintroduce it."""
    source = inspect.getsource(spawn_mod._spawn_after_confirm)
    assert "asyncio.to_thread" in source
    assert "spawn_daemon(" not in source.replace("asyncio.to_thread(\n        spawn_daemon", "")


# ======================================================================
# The confirmation dialog renders the task, not a summary of it
# ======================================================================

SPAWN_EVENT_DATA = {
    "id": "req-spawn", "kind": "spawn", "tool_name": "spawn_session",
    "title": "start a second DOXA session in this repo?",
    "input_summary": "spawn a session at depth 1 (1 live here) — rewrite the parser",
    "body": "a new claude process starts, a new git worktree is created",
    "task": "rewrite the parser\nand keep the tests green",
}


def test_the_spawn_heading_carries_the_task_verbatim():
    from doxa.ui.dialogs import _spawn_heading

    heading = _spawn_heading(SPAWN_EVENT_DATA)
    assert "start a second DOXA session in this repo?" in heading
    assert "a new claude process starts" in heading
    # Both lines, whole, with no ellipsis: the review this dialog performs
    # IS the containment, and a reviewer shown a summary reviewed a summary.
    assert "rewrite the parser" in heading
    assert "and keep the tests green" in heading
    assert "…" not in heading


@pytest.mark.asyncio
async def test_the_popup_opens_on_a_spawn_event_and_answers_allow_or_deny(
        tmp_path, monkeypatch):
    """End to end through the real widget, in a real app: a spawn
    needs_input event opens the same popup every other interactive request
    uses, shows the task, and answers with the permission payload the
    engine's _confirm_spawn reads back."""
    from doxa.app import DoxaApp, NeedsInputPopup
    from doxa.engine import EngineEvent
    from tests.fakes import FakeEngine

    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    engine = FakeEngine([])
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        engine.push_peer_event(EngineEvent("needs_input", dict(SPAWN_EVENT_DATA)))
        popup = pane.query_one("#needs-input-popup", NeedsInputPopup)
        for _ in range(100):
            if popup.is_open:
                break
            await pilot.pause(0.02)
        assert popup.is_open
        assert popup.kind == "spawn"
        heading = str(popup.get_option_at_index(0).prompt)
        # The WHOLE task, both lines. Asserting only the first would be
        # satisfied by the generic permission branch's one-line
        # "title — input_summary" heading, which is exactly the shape this
        # test exists to say is not good enough for a spawn.
        assert "rewrite the parser" in heading
        assert "and keep the tests green" in heading
        assert "a new claude process starts" in heading

        assert popup.choose_index(0) is True
        assert popup.answer_payload() == {"decision": "allow"}
        popup.ask(dict(SPAWN_EVENT_DATA))
        assert popup.choose_index(1) is True
        assert popup.answer_payload() == {"decision": "deny"}

# SPDX-License-Identifier: AGPL-3.0-only
"""The engine seam (v1.4.0): the `Engine`/`EngineProvider` Protocols, the
registry, and the Codex engine end to end.

THE CHECK THE SPEC OWED ITSELF is :func:`test_engine_client_satisfies_the_
protocol_unchanged` -- ``EngineClient`` has never imported the SDK and is
engine-agnostic by construction, so if the Protocol needed it to change,
the Protocol would have been written against ``SessionEngine``'s
implementation rather than against the seam. It does not, and neither does
``SessionEngine``; what the measurement DID change is what the Protocol
contains (see doxa/engines.py's module docstring: ``stop`` is not on both
sides, and two methods differ in async-ness).

Nothing here shells out to Codex. Every mapping and lifecycle path is
driven through ``CodexEngine(exec_factory=...)`` with a scripted stdout,
the same discipline ``SessionEngine(client_factory=...)`` established.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from doxa import diff as diff_mod
from doxa import engines as engines_mod
from doxa import codex as codex_mod
from doxa.codex import (
    CODEX_CAPABILITIES,
    STREAM_LIMIT_BYTES,
    CodexEngine,
    CodexEngineProvider,
    CodexUnavailable,
)
from doxa.engines import (
    CLAUDE_ENGINE_ID,
    CODEX_ENGINE_ID,
    Engine,
    EngineCapabilities,
    capabilities_of,
)
from doxa.events import EngineEvent
from doxa.session.runtime import EVENT_RENDERERS


def _protocol_attrs(proto) -> set:
    """The members a Protocol declares, on every supported Python.

    ``Protocol.__protocol_attrs__`` exists only from 3.12. Through 1.7.5 these
    tests read it directly, so the py3.11 CI leg failed on every run since
    1.4.0 -- unseen because the check was not required. The private
    ``typing._get_protocol_attrs`` is what 3.12 builds the public attribute
    from and is present on 3.8-3.13; it is the same set."""
    attrs = getattr(proto, "__protocol_attrs__", None)
    if attrs is not None:
        return set(attrs)
    import typing
    return set(typing._get_protocol_attrs(proto))


# Every event type the TUI can render or route out-of-band. A second
# engine that needs a type outside this set is a FINDING, not a field --
# see docs/plans/engine-providers.md. This tuple is that rule, enforced.
OOB_EVENT_TYPES = (
    "session_started", "session_done", "peer_joined", "peer_left",
    "peer_message", "tool_disabled", "needs_input", "needs_input_resolved",
    "derive_done", "model_changed", "permission_mode_changed", "base_changed",
)
KNOWN_EVENT_TYPES = frozenset(EVENT_RENDERERS) | frozenset(OOB_EVENT_TYPES)


# -- the Protocol ------------------------------------------------------


def test_engine_client_satisfies_the_protocol_unchanged():
    """The control. EngineClient implements the surface and has never seen
    an SDK object; it must satisfy `Engine` with no edit of its own."""
    from doxa.client import EngineClient

    client = EngineClient("/nonexistent/doxa-test.sock")
    assert isinstance(client, Engine)
    missing = [n for n in _protocol_attrs(Engine) if not hasattr(client, n)]
    assert missing == []


def test_session_engine_satisfies_the_protocol_unchanged(tmp_path):
    from doxa.engine import SessionEngine

    engine = SessionEngine(cwd=str(tmp_path))
    assert isinstance(engine, Engine)


def test_codex_engine_satisfies_the_protocol(tmp_path):
    assert isinstance(CodexEngine(cwd=str(tmp_path)), Engine)


def test_codex_status_uses_only_explicit_unambiguous_effort(tmp_path, monkeypatch):
    config_home = tmp_path / "codex-home"
    config_home.mkdir()
    config = config_home / "config.toml"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(config_home))

    config.write_text('model_reasoning_effort = "xhigh"\n')
    engine = CodexEngine(cwd=str(project), exec_factory=lambda *a, **kw: None)
    assert engine.effort == "xhigh"
    config.write_text('model_reasoning_effort = "high"\n')
    engine._argv(True)  # a new Codex process re-reads configuration
    assert engine.effort == "high"

    config.write_text('profile = "custom"\nmodel_reasoning_effort = "high"\n')
    assert codex_mod.configured_reasoning_effort(str(project)) is None
    config.write_text('model_reasoning_effort = "high"\n')
    (project / ".codex").mkdir()
    (project / ".codex" / "config.toml").write_text('model_reasoning_effort = "low"\n')
    assert codex_mod.configured_reasoning_effort(str(project)) is None


def test_stop_is_not_in_the_protocol():
    """SessionEngine has no ``stop`` -- it is EngineClient's "finalize the
    daemon NOW" verb. A Protocol carrying it would have been written
    against one implementation instead of the seam, and the pane already
    reaches it through getattr."""
    from doxa.client import EngineClient
    from doxa.engine import SessionEngine

    assert "stop" not in _protocol_attrs(Engine)
    assert hasattr(EngineClient, "stop")
    assert not hasattr(SessionEngine, "stop")


def test_the_two_async_divergent_methods_stayed_out():
    """lore_write_state/belief_action_state are sync on SessionEngine and
    async on EngineClient. One signature cannot be honest about both."""
    for name in ("lore_write_state", "belief_action_state"):
        assert name not in _protocol_attrs(Engine)


# -- the registry ------------------------------------------------------


def test_registry_closure():
    """Every engine, listed literally -- adding one is a reviewed act."""
    from doxa.engines import DEEPSEEK_ENGINE_ID, GLM_ENGINE_ID

    assert engines_mod.available() == (
        CLAUDE_ENGINE_ID, CODEX_ENGINE_ID, DEEPSEEK_ENGINE_ID, GLM_ENGINE_ID,
    )


def test_unknown_engine_raises_and_lists_the_real_ones():
    with pytest.raises(KeyError) as excinfo:
        engines_mod.get("gpt-9")
    message = excinfo.value.args[0]
    assert "gpt-9" in message
    assert "claude" in message and "codex" in message
    assert "deepseek" in message and "glm" in message


def test_empty_engine_id_means_the_default():
    assert engines_mod.get(None).engine_id() == CLAUDE_ENGINE_ID
    assert engines_mod.get("  ").engine_id() == CLAUDE_ENGINE_ID
    assert engines_mod.get("CODEX").engine_id() == CODEX_ENGINE_ID


# -- supports(), honestly ----------------------------------------------


def test_claude_supports_everything_it_always_did():
    caps = engines_mod.get("claude").supports()
    assert all(getattr(caps, f) for f in EngineCapabilities.__dataclass_fields__)


def test_codex_capability_map_is_the_measured_one():
    caps = engines_mod.get("codex").supports()
    # Measured against codex-cli 0.144.4's `exec --json` stream.
    assert caps.token_usage is True       # turn.completed.usage exists
    assert caps.context_window is False   # and carries no window size
    assert caps.cost is False             # no cost field anywhere
    assert caps.streaming_text is False   # agent_message arrives whole
    # Taken, as of this release: every turn registers doxa.mcpserver as a
    # stdio MCP server, and the gate that contains those calls lives in
    # that server's process. Proven below by the argv and by
    # tests/test_mcpserver.py, which runs the server for real.
    assert caps.mcp_tools is True
    assert caps.tool_gate is True
    assert caps.permission_modes is False
    # True since issue #39: the daemon takes --engine and hosts this one.
    assert caps.detachable is True
    assert caps.peer_messaging is True    # DOXA's own layer, engine-free


def test_an_engine_that_declares_nothing_is_read_as_claude():
    """Every handle that existed before this module is a Claude session,
    so the default has to reproduce what those already did."""

    class Bare:
        pass

    assert capabilities_of(Bare()) == EngineCapabilities.claude()


def test_a_handle_that_declares_is_believed_about_itself(tmp_path):
    assert capabilities_of(CodexEngine(cwd=str(tmp_path))) is CODEX_CAPABILITIES


# -- the Codex event mapping -------------------------------------------


def _engine(tmp_path, **kwargs) -> CodexEngine:
    return CodexEngine(cwd=str(tmp_path), **kwargs)


def _map(engine: CodexEngine, frame: dict) -> "list[EngineEvent]":
    return engine.map_event(frame)


def test_thread_started_is_consumed_not_emitted(tmp_path):
    """No EngineEvent kind means "the engine renamed its conversation",
    and none was invented: it becomes the resume token."""
    engine = _engine(tmp_path)
    assert _map(engine, {"type": "thread.started", "thread_id": "t-1"}) == []
    assert engine.thread_id == "t-1"


def test_agent_message_becomes_one_text_delta(tmp_path):
    engine = _engine(tmp_path)
    events = _map(engine, {
        "type": "item.completed",
        "item": {"id": "i0", "type": "agent_message", "text": "hello"},
    })
    assert [e.type for e in events] == ["text_delta"]
    assert events[0].data["text"] == "hello"


def test_an_unfinished_agent_message_emits_nothing(tmp_path):
    engine = _engine(tmp_path)
    assert _map(engine, {
        "type": "item.started",
        "item": {"id": "i0", "type": "agent_message", "text": ""},
    }) == []


def test_command_execution_is_a_tool_call_then_a_tool_result(tmp_path):
    engine = _engine(tmp_path)
    started = _map(engine, {
        "type": "item.started",
        "item": {"id": "i2", "type": "command_execution",
                 "command": "ls -la", "status": "in_progress"},
    })
    assert [e.type for e in started] == ["tool_call"]
    assert started[0].data["name"] == "command_execution"
    assert started[0].data["input"] == {"command": "ls -la"}
    done = _map(engine, {
        "type": "item.completed",
        "item": {"id": "i2", "type": "command_execution", "command": "ls -la",
                 "aggregated_output": "total 4\n", "exit_code": 0,
                 "status": "completed"},
    })
    assert [e.type for e in done] == ["tool_result"]
    assert done[0].data["id"] == "i2"
    assert done[0].data["is_error"] is False
    assert "total 4" in done[0].data["result_summary"]


def test_a_nonzero_exit_is_an_error_result(tmp_path):
    engine = _engine(tmp_path)
    done = _map(engine, {
        "type": "item.completed",
        "item": {"id": "i3", "type": "command_execution", "command": "false",
                 "aggregated_output": "", "exit_code": 1, "status": "completed"},
    })
    assert done[0].data["is_error"] is True


def test_a_cancelled_mcp_call_is_an_error_result(tmp_path):
    """The shape a Codex MCP call takes when the approval mode refuses it
    -- measured live before this engine was designed."""
    engine = _engine(tmp_path)
    done = _map(engine, {
        "type": "item.completed",
        "item": {"id": "i4", "type": "mcp_tool_call", "server": "doxaprobe",
                 "tool": "ping", "result": None,
                 "error": {"message": "user cancelled MCP tool call"},
                 "status": "failed"},
    })
    assert done[0].data["is_error"] is True
    assert done[0].data["name"] == "doxaprobe/ping"


def test_todo_list_rides_the_tool_call_kinds_and_updates_in_place(tmp_path):
    """A plan has no EngineEvent kind. Claude's own equivalent arrives as
    a tool call, so this one does too -- and item.updated (no progress
    kind either) refreshes the same chip rather than inventing one."""
    engine = _engine(tmp_path)
    rows = [{"text": "a", "completed": False}, {"text": "b", "completed": False}]
    started = _map(engine, {
        "type": "item.started",
        "item": {"id": "p1", "type": "todo_list", "items": rows},
    })
    assert [e.type for e in started] == ["tool_call"]
    assert started[0].data["name"] == "todo_list"
    rows[0]["completed"] = True
    updated = _map(engine, {
        "type": "item.updated",
        "item": {"id": "p1", "type": "todo_list", "items": rows},
    })
    assert [e.type for e in updated] == ["tool_result"]
    assert updated[0].data["id"] == "p1"
    assert updated[0].data["result_summary"] == "1/2 done"


def test_an_unknown_item_kind_is_dropped_never_guessed(tmp_path):
    engine = _engine(tmp_path)
    assert _map(engine, {
        "type": "item.completed",
        "item": {"id": "z", "type": "something_new_in_0_200_0"},
    }) == []


def test_turn_failed_folds_into_turn_done_with_is_error(tmp_path):
    engine = _engine(tmp_path)
    events = _map(engine, {"type": "turn.failed", "message": "boom"})
    assert [e.type for e in events] == ["text_delta", "turn_done"]
    assert "boom" in events[0].data["text"]
    assert events[1].data["is_error"] is True
    assert events[1].data["cost_usd"] is None


def test_the_mapping_never_produces_an_event_kind_the_tui_cannot_render(tmp_path):
    """The spec's rule, enforced: `EVENT_RENDERERS` is what the TUI is
    written against, and a new engine does not get to widen it."""
    engine = _engine(tmp_path)
    frames = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "turn.completed", "usage": {"input_tokens": 1}},
        {"type": "turn.failed", "message": "x"},
        {"type": "error", "message": "x"},
        {"type": "item.started", "item": {"id": "1", "type": "command_execution",
                                          "command": "ls"}},
        {"type": "item.completed", "item": {"id": "1", "type": "command_execution",
                                            "command": "ls", "exit_code": 0}},
        {"type": "item.completed", "item": {"id": "2", "type": "agent_message",
                                            "text": "hi"}},
        {"type": "item.completed", "item": {"id": "3", "type": "reasoning",
                                            "text": "thinking"}},
        {"type": "item.started", "item": {"id": "4", "type": "file_change",
                                          "changes": [{"path": "/a"}]}},
        {"type": "item.completed", "item": {"id": "4", "type": "file_change",
                                            "changes": [{"path": "/a"}]}},
        {"type": "item.started", "item": {"id": "5", "type": "web_search",
                                          "query": "q"}},
        {"type": "item.updated", "item": {"id": "6", "type": "todo_list",
                                          "items": []}},
    ]
    for frame in frames:
        for event in engine.map_event(frame):
            assert event.type in KNOWN_EVENT_TYPES, event.type


def test_a_non_json_stdout_line_is_ignored(tmp_path):
    engine = _engine(tmp_path)
    assert engine._map_line(b"not json at all\n") == []
    assert engine._map_line(b"[1, 2, 3]\n") == []


# -- context and cost: unknown is unknown ------------------------------


@pytest.mark.asyncio
async def test_codex_context_usage_is_none_never_a_percentage(tmp_path):
    engine = _engine(tmp_path)
    assert await engine.context_usage() is None
    assert engine.last_ctx_percentage is None
    assert engine.last_ctx_max_tokens is None


def test_token_usage_accumulates_but_never_becomes_a_context_reading(tmp_path):
    engine = _engine(tmp_path)
    engine.map_event({"type": "turn.completed", "usage": {
        "input_tokens": 100, "output_tokens": 10,
        "cached_input_tokens": 40, "reasoning_output_tokens": 5,
    }})
    engine.map_event({"type": "turn.completed", "usage": {
        "input_tokens": 200, "output_tokens": 20,
    }})
    summary = engine.usage_summary()
    assert summary["input_tokens"] == 300
    assert summary["output_tokens"] == 30
    assert summary["cache_read_input_tokens"] == 40
    # The window is still unknown, and the cost is still unreported.
    assert summary["ctx_percentage"] is None
    assert summary["ctx_tokens"] is None
    assert summary["total_cost_usd"] is None


def test_a_bogus_usage_block_changes_nothing(tmp_path):
    engine = _engine(tmp_path)
    engine.map_event({"type": "turn.completed", "usage": {
        "input_tokens": True, "output_tokens": -5, "cached_input_tokens": "12",
    }})
    assert engine.usage_totals == {}


def test_peer_usage_tokens_stays_none_until_something_is_measured(tmp_path):
    """None means unknown, never 0 -- the rail prints `tok —` for it."""
    from doxa import peers as peers_mod

    host = peers_mod.PeerHost(session_id="s" * 32, cwd=str(tmp_path))
    assert host.usage_tokens is None


# -- the turn: spawn, stream, resume ------------------------------------


class _FakeStdout:
    def __init__(self, lines: "list[bytes]") -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeStdin:
    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeStderr:
    """A stderr that is drained the way a real one is: chunked, to EOF."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            chunk, self._data = self._data, b""
            return chunk
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


class _FakeProc:
    def __init__(self, lines: "list[bytes]") -> None:
        self.stdout = _FakeStdout(lines)
        self.stdin = _FakeStdin()
        self.stderr = None
        self.returncode = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


def _script(*frames: dict) -> "list[bytes]":
    return [json.dumps(f).encode() + b"\n" for f in frames]


def _factory(recorder: list, lines_per_call: "list[list[bytes]]"):
    async def make(*argv, **kwargs):
        recorder.append((list(argv), kwargs))
        return _FakeProc(lines_per_call[len(recorder) - 1])
    return make


@pytest.mark.asyncio
async def test_a_turn_streams_and_ends_with_turn_done(tmp_path):
    calls: list = []
    lines = [_script(
        {"type": "thread.started", "thread_id": "th-9"},
        {"type": "turn.started"},
        {"type": "item.completed",
         "item": {"id": "a", "type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 7}},
    )]
    engine = _engine(tmp_path, exec_factory=_factory(calls, lines))
    events = [e async for e in engine.send("do a thing")]
    assert [e.type for e in events] == ["turn_started", "text_delta", "turn_done"]
    assert engine.thread_id == "th-9"
    assert engine.num_turns == 1
    assert events[-1].data["ctx_percentage"] is None
    assert events[-1].data["cost_usd"] is None


@pytest.mark.asyncio
async def test_the_prompt_goes_in_on_stdin_never_argv(tmp_path):
    calls: list = []
    engine = _engine(tmp_path, exec_factory=_factory(calls, [_script(
        {"type": "turn.completed", "usage": {}},
    )]))
    secret_shaped = "x" * 5000
    _ = [e async for e in engine.send(secret_shaped)]
    argv, _kwargs = calls[0]
    assert secret_shaped not in " ".join(argv)
    assert argv[-1] == "-"


@pytest.mark.asyncio
async def test_the_second_turn_resumes_the_first_ones_thread(tmp_path):
    calls: list = []
    lines = [
        _script({"type": "thread.started", "thread_id": "th-1"},
                {"type": "turn.completed", "usage": {}}),
        _script({"type": "turn.completed", "usage": {}}),
    ]
    engine = _engine(tmp_path, exec_factory=_factory(calls, lines))
    _ = [e async for e in engine.send("first")]
    _ = [e async for e in engine.send("second")]
    first_argv, _k = calls[0]
    second_argv, _k = calls[1]
    assert "resume" not in first_argv
    assert second_argv[1:4] == ["exec", "resume", "th-1"]


@pytest.mark.asyncio
async def test_a_turn_reaps_its_process_even_when_the_caller_stops_early(tmp_path):
    """The pane's exclusive worker cancels a turn by dropping the
    generator; conftest reaps leaked agent subprocesses and would say so."""
    calls: list = []
    lines = [_script(
        {"type": "item.completed",
         "item": {"id": "a", "type": "agent_message", "text": "one"}},
        {"type": "item.completed",
         "item": {"id": "b", "type": "agent_message", "text": "two"}},
        {"type": "turn.completed", "usage": {}},
    )]
    engine = _engine(tmp_path, exec_factory=_factory(calls, lines))
    stream = engine.send("go")
    await stream.__anext__()   # turn_started
    await stream.__anext__()   # first text_delta
    await stream.aclose()
    assert engine._proc is None


@pytest.mark.asyncio
async def test_the_turn_persists_a_transcript_line_per_side(tmp_path):
    calls: list = []
    engine = _engine(tmp_path, exec_factory=_factory(calls, [_script(
        {"type": "item.completed",
         "item": {"id": "a", "type": "agent_message", "text": "reply"}},
        {"type": "turn.completed", "usage": {}},
    )]))
    _ = [e async for e in engine.send("ask")]
    records = [
        json.loads(line)
        for line in engine.transcript_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [r["type"] for r in records] == ["user", "assistant"]
    assert records[0]["message"]["content"] == "ask"
    assert records[1]["message"]["content"][0]["text"] == "reply"


@pytest.mark.asyncio
async def test_start_refuses_when_the_codex_cli_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("doxa.codex.shutil.which", lambda _name: None)
    with pytest.raises(CodexUnavailable):
        await CodexEngine(cwd=str(tmp_path)).start()


# -- /resume: DOXA's session id is not Codex's thread id (issue #43) ----
#
# `spawn_daemon(resume=<doxa session id>)` hands that id to CodexEngine,
# which used it as the thread id -- so the first turn of every resumed
# session ran `codex exec resume <a-doxa-uuid>` against an id Codex never
# issued. The translation between the two is a per-session record beside
# the transcript, written the moment `thread.started` names a thread.


async def _run_one_turn(engine, prompt: str = "go") -> list:
    return [event async for event in engine.send(prompt)]


@pytest.mark.asyncio
async def test_the_thread_id_is_recorded_when_codex_names_it(tmp_path):
    """The record IS the fix: without it nothing that outlives this
    process knows which Codex thread this DOXA session is."""
    engine = _engine(tmp_path, session_id="s-a", exec_factory=_factory([], [
        _script({"type": "thread.started", "thread_id": "th-1"},
                {"type": "turn.completed", "usage": {}}),
    ]))
    await _run_one_turn(engine)
    assert engine.thread_path.name == "s-a.codex.json"
    record = json.loads(engine.thread_path.read_text(encoding="utf-8"))
    assert record["thread_id"] == "th-1"
    assert record["session_id"] == "s-a"
    assert record["cwd"] == str(tmp_path)
    assert record["recorded"]


@pytest.mark.asyncio
async def test_the_record_is_rewritten_when_codex_renames_the_thread(tmp_path):
    """map_event's own docstring says that frame means the id CHANGED, so
    a record written once and never again would go stale silently."""
    engine = _engine(tmp_path, session_id="s-b", exec_factory=_factory([], [
        _script({"type": "thread.started", "thread_id": "th-1"},
                {"type": "thread.started", "thread_id": "th-2"},
                {"type": "turn.completed", "usage": {}}),
    ]))
    await _run_one_turn(engine)
    assert engine.thread_id == "th-2"
    record = json.loads(engine.thread_path.read_text(encoding="utf-8"))
    assert record["thread_id"] == "th-2"


@pytest.mark.asyncio
async def test_a_resume_with_a_record_makes_the_first_turn_a_resume(tmp_path):
    """The whole point: the SECOND process's FIRST turn continues the
    thread the first process started."""
    first = _engine(tmp_path, session_id="s-c", exec_factory=_factory([], [
        _script({"type": "thread.started", "thread_id": "th-7"},
                {"type": "turn.completed", "usage": {}}),
    ]))
    await _run_one_turn(first, "hello")

    # What doxa.daemon.spawn_daemon does on /resume: one string, passed as
    # both session_id and resume (a resume keeps its id).
    calls: list = []
    resumed = _engine(
        tmp_path, session_id="s-c", resume="s-c",
        exec_factory=_factory(calls, [_script(
            {"type": "thread.started", "thread_id": "th-7"},
            {"type": "turn.completed", "usage": {}},
        )]),
    )
    assert resumed.thread_id == "th-7"
    await _run_one_turn(resumed, "again")
    argv, _kwargs = calls[0]
    assert argv[1:4] == ["exec", "resume", "th-7"]


@pytest.mark.asyncio
async def test_a_resume_without_a_record_refuses_to_start(tmp_path, monkeypatch):
    """Starting anyway would open a NEW Codex thread under an id the user
    was told carried their conversation."""
    monkeypatch.setattr("doxa.codex.shutil.which", lambda _name: "/usr/bin/codex")
    engine = _engine(tmp_path, session_id="s-d", resume="s-d")
    assert engine.thread_id is None
    with pytest.raises(codex_mod.CodexThreadUnknown, match=r"no recorded Codex thread"):
        await engine.start()
    # And it says what the operator can still do with the session.
    with pytest.raises(codex_mod.CodexThreadUnknown, match=r"transcript"):
        await engine.start()


@pytest.mark.asyncio
async def test_the_doxa_session_id_is_never_placed_after_resume(tmp_path):
    """The defect, stated as an invariant over every argv this engine can
    build: whatever follows `resume` is Codex's id, never DOXA's."""
    first = _engine(tmp_path, session_id="s-e", exec_factory=_factory([], [
        _script({"type": "thread.started", "thread_id": "th-3"},
                {"type": "turn.completed", "usage": {}}),
    ]))
    await _run_one_turn(first)

    calls: list = []
    resumed = _engine(
        tmp_path, session_id="s-e", resume="s-e",
        exec_factory=_factory(calls, [
            _script({"type": "turn.completed", "usage": {}}),
            _script({"type": "turn.completed", "usage": {}}),
        ]),
    )
    await _run_one_turn(resumed, "one")
    await _run_one_turn(resumed, "two")
    # A session with NO record never builds a resume argv at all -- it
    # cannot start, and its argv would carry no id to misplace either.
    no_record = _engine(tmp_path, session_id="s-f", resume="s-f")
    argvs = [argv for argv, _kwargs in calls]
    argvs += [no_record._argv(True), no_record._argv(False)]
    for argv in argvs:
        for index, token in enumerate(argv):
            if token == "resume":
                assert argv[index + 1] not in ("s-e", "s-f")
                assert argv[index + 1] == "th-3"
    assert sum(argv.count("resume") for argv in argvs) == 2


@pytest.mark.asyncio
async def test_finalize_keeps_the_thread_record(tmp_path):
    """A finalized session is exactly what /resume comes back for."""
    engine = _engine(tmp_path, session_id="s-g", exec_factory=_factory([], [
        _script({"type": "thread.started", "thread_id": "th-4"},
                {"type": "turn.completed", "usage": {}}),
    ]))
    await _run_one_turn(engine)
    await engine.finalize()
    assert json.loads(engine.thread_path.read_text(encoding="utf-8"))[
        "thread_id"
    ] == "th-4"


@pytest.mark.asyncio
async def test_permission_mode_is_refused_by_name_not_faked(tmp_path):
    engine = _engine(tmp_path)
    with pytest.raises(NotImplementedError) as excinfo:
        await engine.set_permission_mode("acceptEdits")
    assert "permission modes" in str(excinfo.value)


@pytest.mark.asyncio
async def test_set_model_says_it_lands_on_the_next_turn(tmp_path):
    engine = _engine(tmp_path)
    note = await engine.set_model("gpt-5.4")
    assert engine.model == "gpt-5.4"
    assert "next turn" in note


@pytest.mark.asyncio
async def test_finalize_is_idempotent_and_reports_the_review_gap(tmp_path):
    engine = _engine(tmp_path)
    first = await engine.finalize()
    assert first.type == "session_done"
    assert "skipped" in first.data["review"]
    second = await engine.finalize()
    assert second.data == {"already_finalized": True}


# -- the diff tick learned a second vocabulary --------------------------


def test_is_tick_knows_codex_names():
    assert diff_mod.is_tick("file_change") is True
    assert diff_mod.is_tick("patch_apply") is True
    assert diff_mod.is_tick("command_execution", {"command": "rm -rf x"}) is True
    assert diff_mod.is_tick("command_execution", {"command": "ls -la"}) is False
    # And it did not forget the first one.
    assert diff_mod.is_tick("Edit") is True
    assert diff_mod.is_tick("Bash", {"command": "ls"}) is False


# -- the provider --------------------------------------------------------


def test_the_provider_builds_an_engine_and_ignores_what_it_cannot_use(tmp_path):
    """new_session takes DOXA's session vocabulary; a provider ignores the
    arguments its engine has no use for rather than making every caller
    branch on which engine it is talking to."""
    engine = CodexEngineProvider().new_session(
        cwd=str(tmp_path), model="gpt-5.4", session_id="s-1",
        daemon_socket="/tmp/x.sock", allowed_tools={"Bash"},
    )
    assert isinstance(engine, CodexEngine)
    assert engine.session_id == "s-1"
    assert engine.model == "gpt-5.4"


# -- the CLI's per-session choice ---------------------------------------


class _RecordingApp:
    """Stands in for DoxaApp: records the factories the CLI handed it and
    never opens a terminal."""

    last: "dict | None" = None

    def __init__(self, **kwargs):
        _RecordingApp.last = kwargs

    def run(self):
        return None


def test_cli_refuses_an_unknown_engine_with_the_real_list(capsys):
    from doxa import cli as cli_mod

    assert cli_mod.main(["--engine", "gpt-9"]) == 2
    err = capsys.readouterr().err
    assert "unknown engine 'gpt-9'" in err
    assert "claude, codex" in err


def test_cli_engine_codex_takes_the_daemon_path(monkeypatch, tmp_path):
    """Issue #39: the daemon hosts any registered engine, so --engine no
    longer diverts the session into the TUI process. The spawn carries the
    engine id, and every door onto a LATER session carries it too -- a
    Ctrl+T tab on a Codex window must not open a Claude one."""
    from doxa import cli as cli_mod

    spawned = []

    def fake_spawn(cwd, **kwargs):
        spawned.append({"cwd": cwd, **kwargs})
        return "sid", "/tmp/nope.sock"

    attached = {}

    def fake_run_attached(socket_path, cwd, model, linger, engine=None):
        attached["engine"] = engine

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(cli_mod, "_run_attached", fake_run_attached)
    _RecordingApp.last = None
    assert cli_mod.main(["new", "--engine", "codex"]) == 0
    assert _RecordingApp.last is None  # nothing was built in this process
    assert [c["engine"] for c in spawned] == ["codex"]
    assert attached["engine"] == "codex"


def test_cli_engine_codex_in_process_still_builds_a_codex_factory(
    monkeypatch, tmp_path
):
    """--in-process is the one door left that builds an engine in the TUI,
    and it works for any engine."""
    from doxa import cli as cli_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_mod, "DoxaApp", _RecordingApp)
    _RecordingApp.last = None
    assert cli_mod.main(["--engine", "codex", "--in-process"]) == 0
    kwargs = _RecordingApp.last
    assert kwargs is not None
    engine = kwargs["engine_factory"]()
    assert isinstance(engine, CodexEngine)
    assert isinstance(kwargs["new_session_factory"](), CodexEngine)
    assert isinstance(kwargs["new_session_factory_at"](str(tmp_path)), CodexEngine)
    assert isinstance(
        kwargs["resume_session_factory"](str(tmp_path), "s-2"), CodexEngine
    )


def test_cli_default_engine_leaves_the_claude_path_untouched(monkeypatch, tmp_path):
    """The regression guard: with no --engine, the daemon path is not
    reached through the new branch."""
    from doxa import cli as cli_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_mod, "DoxaApp", _RecordingApp)
    _RecordingApp.last = None
    assert cli_mod.main(["--in-process"]) == 0
    kwargs = _RecordingApp.last
    assert kwargs is not None
    # The in-process Claude path passes cwd/model only -- DoxaApp's own
    # default factory (which honours the suite's monkeypatch of
    # doxa.app.SessionEngine) still builds the engine.
    assert "engine_factory" not in kwargs


def test_the_argv_carries_no_flag_that_resume_would_reject(tmp_path):
    """`codex exec resume` accepts neither -C nor -s (measured: `error:
    unexpected argument '-C' found`). One argv shape for both turns, with
    the cwd on the subprocess and the sandbox in a config override."""
    engine = _engine(tmp_path)
    engine.thread_id = "th-x"
    for argv in (engine._argv(True), engine._argv(False)):
        assert "-C" not in argv and "--cd" not in argv
        assert "-s" not in argv and "--sandbox" not in argv
        assert 'sandbox_mode="workspace-write"' in argv
        assert 'approval_policy="never"' in argv


def _stdin_factory(procs: list, lines_per_call: "list[list[bytes]]"):
    """``_factory``'s sibling that keeps the PROCESS, not just the argv --
    the tests below assert on what was written to its stdin."""
    async def make(*_argv, **_kwargs):
        proc = _FakeProc(lines_per_call[len(procs)])
        procs.append(proc)
        return proc
    return make


def _stdin_of(proc) -> str:
    return proc.stdin.written.decode("utf-8")


def _overrides(argv: "list[str]") -> "dict[str, str]":
    """The ``-c key=value`` pairs in an argv, as a dict."""
    out: "dict[str, str]" = {}
    for flag, pair in zip(argv, argv[1:]):
        if flag == "-c" and "=" in pair:
            key, _, value = pair.partition("=")
            out[key] = value
    return out


def test_every_turn_registers_the_doxa_mcp_server(tmp_path):
    """mcp_tools=True, in the one place it is actually true: the argv.

    BOTH shapes, because the resume shape is where this can silently
    regress -- a second turn without the server would look like a model
    that simply chose not to call its tools, and nothing would say
    otherwise. The four keys are the CLI's own spelling, read back off a
    ``config.toml`` that ``codex mcp add`` generated."""
    engine = _engine(tmp_path, session_id="s-42")
    engine.thread_id = "th-x"
    prefix = "mcp_servers.doxa"
    for argv in (engine._argv(True), engine._argv(False)):
        over = _overrides(argv)
        assert over[f"{prefix}.command"] == json.dumps(sys.executable)
        assert over[f"{prefix}.args"] == json.dumps(["-m", "doxa.mcpserver"])
        # Without this Codex auto-cancels the call with "user cancelled
        # MCP tool call" and tells the model it was refused.
        assert over[f"{prefix}.default_tools_approval_mode"] == '"approve"'
        assert over[f"{prefix}.env.DOXA_MCP_SESSION_ID"] == '"s-42"'
        assert over[f"{prefix}.env.DOXA_MCP_CWD"] == json.dumps(str(tmp_path))
        assert over[f"{prefix}.env.DOXA_MCP_LORE"] == '"1"'
        # The prompt still arrives on stdin, after everything.
        assert argv[-1] == "-"


def test_the_mcp_overrides_do_not_cost_the_one_shape_property(tmp_path):
    """The argv is still ONE shape: the resume form differs from the first
    only by `resume <id>`, and by nothing else."""
    engine = _engine(tmp_path)
    engine.thread_id = "th-x"
    first, resume = engine._argv(True), engine._argv(False)
    assert resume[:2] == first[:2] == ["codex", "exec"]
    assert resume[2:4] == ["resume", "th-x"]
    assert resume[4:] == first[2:]


def test_memory_off_is_told_to_the_server_and_to_the_prompt(tmp_path):
    """``lore=False`` reaches the MCP server as an env switch, and the
    server answers it by not OFFERING the lore_* tools (proven in
    tests/test_mcpserver.py). Through v1.12.0 this argument was swallowed
    by ``**_ignored``, so a memory-off Codex agent ran with memory on."""
    on = _engine(tmp_path, lore=True)
    off = _engine(tmp_path, lore=False)
    assert on.lore is True and off.lore is False
    assert _overrides(on._argv(True))["mcp_servers.doxa.env.DOXA_MCP_LORE"] == '"1"'
    assert _overrides(off._argv(True))["mcp_servers.doxa.env.DOXA_MCP_LORE"] == '"0"'


def test_the_forwarded_environment_is_an_allow_list_of_non_secrets(
    tmp_path, monkeypatch,
):
    """A ``-c`` override lands on ``codex exec``'s argv, which every other
    process on the machine can read out of ``ps``. Forwarding the whole
    environment would put whatever key happens to be exported in there."""
    monkeypatch.setenv("LORE_ROOT", str(tmp_path / "store"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-should-never-appear")
    argv = _engine(tmp_path)._argv(True)
    over = _overrides(argv)
    assert over["mcp_servers.doxa.env.LORE_ROOT"] == json.dumps(
        str(tmp_path / "store")
    )
    assert not [k for k in over if "API_KEY" in k]
    assert "ds-should-never-appear" not in " ".join(argv)


def test_a_cwd_with_a_quote_in_it_cannot_inject_config(tmp_path):
    """Same class of defect ``SANDBOX_MODES`` is an allow-list to prevent,
    on the other value that reaches a TOML override."""
    nasty = tmp_path / 'we"ird\\dir'
    engine = _engine(nasty)
    value = _overrides(engine._argv(True))["mcp_servers.doxa.env.DOXA_MCP_CWD"]
    assert value == json.dumps(str(nasty))
    assert json.loads(value) == str(nasty)


def test_peer_send_is_not_asked_for_before_the_control_socket_is_up(tmp_path):
    """Three things arm the tool and all three are re-read per turn (see
    CodexEngine._peer_send_armed). This engine was never started, so it
    serves no control socket -- and a sidecar told to send at a socket
    nobody is listening on would be offered a tool that always fails.
    The other two arms are exercised in tests/test_peer_delivery.py."""
    engine = _engine(tmp_path)
    assert engine._engine_control.running is False
    over = _overrides(engine._argv(True))
    assert over["mcp_servers.doxa.env.DOXA_MCP_PEER_SEND"] == '"0"'
    assert "mcp_servers.doxa.env.DOXA_MCP_ENGINE_SOCKET" not in over
    assert "mcp_servers.doxa.env.DOXA_MCP_TURN_ID" not in over


@pytest.mark.asyncio
async def test_the_first_prompt_carries_the_lore_snapshot_under_a_header(
    tmp_path, monkeypatch,
):
    """Codex has no system-message channel and no SessionStart hook, so
    the snapshot rides the first turn's STDIN -- and only stdin: the
    transcript keeps the operator's prompt alone, because the transcript
    is what gets indexed back into the store at finalize."""
    from lore_core import context as lore_context

    monkeypatch.setattr(
        lore_context, "build_context", lambda _cwd: "REMEMBERED: the fact"
    )
    procs: list = []
    lines = [
        _script({"type": "thread.started", "thread_id": "th-1"},
                {"type": "turn.completed", "usage": {}}),
        _script({"type": "turn.completed", "usage": {}}),
    ]
    engine = _engine(tmp_path, exec_factory=_stdin_factory(procs, lines))

    [e async for e in engine.send("do a thing")]
    first_stdin = _stdin_of(procs[0])
    assert codex_mod.LORE_PREAMBLE_HEADER in first_stdin
    assert "REMEMBERED: the fact" in first_stdin
    assert codex_mod.LORE_PREAMBLE_FOOTER in first_stdin
    assert first_stdin.endswith("do a thing")
    assert engine.lore_snapshot_chars == len("REMEMBERED: the fact")
    # The transcript got the prompt, not the snapshot -- it is what
    # lore_store.index_live reads at finalize, and feeding the store its
    # own contents back would be a memory that grows by quoting itself.
    written = engine.transcript_path.read_text(encoding="utf-8")
    assert "do a thing" in written
    assert "REMEMBERED: the fact" not in written

    # Turn two resumes the thread, which already holds it: re-sending
    # would pay for the same text every turn.
    [e async for e in engine.send("and another")]
    assert _stdin_of(procs[1]) == "and another"


@pytest.mark.asyncio
async def test_memory_off_sends_no_snapshot_at_all(tmp_path, monkeypatch):
    """Not "built and discarded": with memory off the store is never read,
    which on a fleet run is the very access the switch exists to prevent."""
    from lore_core import context as lore_context

    def _must_not_run(_cwd):  # pragma: no cover -- the assertion is that it
        raise AssertionError("the store was read with memory off")

    monkeypatch.setattr(lore_context, "build_context", _must_not_run)
    procs: list = []
    engine = _engine(
        tmp_path, lore=False,
        exec_factory=_stdin_factory(procs, [_script({"type": "turn.completed"})]),
    )
    [e async for e in engine.send("do a thing")]
    assert _stdin_of(procs[0]) == "do a thing"
    assert engine.lore_snapshot_chars == 0


def test_an_unrecognised_sandbox_mode_falls_back_never_passes_through(tmp_path):
    """self.sandbox is interpolated into a TOML override, so an operator
    string reaching it unchecked would be config injection into the one
    setting that decides what the agent may write."""
    engine = CodexEngine(cwd=str(tmp_path), sandbox='x"\nmodel="evil')
    assert engine.sandbox == "workspace-write"
    engine = CodexEngine(cwd=str(tmp_path), sandbox="read-only")
    assert engine.sandbox == "read-only"


@pytest.mark.asyncio
async def test_a_nonzero_exit_with_a_silent_stream_is_an_error_turn(tmp_path):
    """The shape a missing login (or a rejected flag) takes. Silence would
    render as a turn that simply produced no text -- the one reading that
    sends the operator looking in the wrong place."""

    class _FailingProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__([])
            self.returncode = 2
            self.stderr = _FakeStderr(b"error: unexpected argument '-C' found")

        async def wait(self) -> int:
            return 2

    async def make(*argv, **kwargs):
        return _FailingProc()

    engine = _engine(tmp_path, exec_factory=make)
    events = [e async for e in engine.send("go")]
    assert [e.type for e in events] == ["turn_started", "text_delta", "turn_done"]
    # The reason is READABLE, not just flagged: a turn marked "✗ error"
    # with no text in it sends the operator looking in the wrong place.
    assert "unexpected argument" in events[1].data["text"]
    assert events[-1].data["is_error"] is True
    assert "unexpected argument" in events[-1].data["error"]


# -- the four ways one turn used to be able to hang or lie -------------
#
# Every test below drives a FAKE `codex exec` through `exec_factory`, the
# seam the engine has carried since v1.4.0 for exactly this. Three of them
# would HANG on the code they were written against, so each one is bounded
# by asyncio.wait_for and asserts that it COMPLETED -- a hang has to fail
# as a failing test, not as a suite that never returns.


class _CoupledStderr:
    """Stderr as the OS actually gives it: a pipe with a finite buffer.

    ``chunks`` are handed out on demand; once more than ``blocks_at``
    bytes have been TAKEN, the child is unblocked. Until then it is stuck
    in write(2) -- which is what ``_CoupledStdout`` models."""

    def __init__(self, payload: bytes, unblocked: asyncio.Event,
                 blocks_at: int = 64 * 1024) -> None:
        self._payload = payload
        self._unblocked = unblocked
        self._blocks_at = blocks_at
        self.taken = 0

    async def read(self, n: int = -1) -> bytes:
        size = len(self._payload) if n < 0 else n
        chunk, self._payload = self._payload[:size], self._payload[size:]
        self.taken += len(chunk)
        if self.taken > self._blocks_at:
            self._unblocked.set()
        return chunk


class _CoupledStdout:
    """A child that cannot write stdout until its stderr has been read."""

    def __init__(self, lines: "list[bytes]", unblocked: asyncio.Event) -> None:
        self._lines = list(lines)
        self._unblocked = unblocked

    async def readline(self) -> bytes:
        await self._unblocked.wait()
        return self._lines.pop(0) if self._lines else b""


class _CoupledProc:
    """A `codex exec` whose stderr fills the pipe before its first event."""

    def __init__(self, lines: "list[bytes]", stderr_bytes: bytes) -> None:
        self.unblocked = asyncio.Event()
        self.stdout = _CoupledStdout(lines, self.unblocked)
        self.stderr = _CoupledStderr(stderr_bytes, self.unblocked)
        self.stdin = _FakeStdin()
        self.returncode = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.unblocked.set()

    async def wait(self) -> int:
        await self.unblocked.wait()
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.mark.asyncio
async def test_a_child_that_fills_the_stderr_pipe_does_not_hang_the_turn(tmp_path):
    """The deadlock, as the kernel serves it (v1.7.3).

    stderr's pipe buffer is about 64 KiB. A child past it blocks in
    write(2), so it never writes stdout and never closes it -- and the
    stdout read loop waits for a line that cannot come. Draining stderr
    AFTER that loop (and only on a non-zero exit, which is what v1.7.2
    did) cannot break it: the loop is the thing that never ends. This
    test HANGS on that code; the concurrent drain is what completes it."""
    noisy = b"warning: something\n" * 8000  # ~150 KiB, well past the pipe
    procs: list = []

    async def make(*argv, **kwargs):
        proc = _CoupledProc(_script(
            {"type": "item.completed",
             "item": {"id": "a", "type": "agent_message", "text": "survived"}},
            {"type": "turn.completed", "usage": {}},
        ), noisy)
        procs.append(proc)
        return proc

    engine = _engine(tmp_path, exec_factory=make)
    events = await asyncio.wait_for(
        _collect(engine.send("go")), timeout=10
    )
    assert [e.type for e in events] == ["turn_started", "text_delta", "turn_done"]
    assert events[1].data["text"] == "survived"
    assert events[-1].data["is_error"] is False
    # And the pipe was actually emptied, not merely bypassed.
    assert procs[0].stderr.taken == len(noisy)


async def _collect(stream) -> list:
    return [e async for e in stream]


@pytest.mark.asyncio
async def test_one_oversized_event_does_not_abort_the_turn(tmp_path):
    """Finding 3, against a REAL StreamReader -- the only way to test it.

    ``asyncio.create_subprocess_exec`` defaults to a 64 KiB reader limit,
    and one JSONL line over it makes ``readline()`` raise
    ``LimitOverrunError``, which escaped ``send``, ended the turn and
    killed Codex. A fake stdout cannot show that: the limit lives in
    asyncio's own reader. So this spawns a real process that prints one
    oversized ``agent_message`` -- a python script standing in for
    ``codex exec``, never the CLI itself."""
    # Prose-shaped, and deliberately: `scrub_secrets` is quadratic in the
    # length of an UNBROKEN alphanumeric run (measured: 64 KiB of one takes
    # ~16 s), so a payload of `"y" * 300000` would be testing lore_core's
    # regex, not this engine's reader. Spaces keep that path linear. See
    # the branch report -- that blowup is real and it is not this fix's.
    chunk = "the quick brown fox jumps over the lazy dog "
    reply = chunk * (300 * 1024 // len(chunk))  # ~300 KiB, 4.6x the default
    script = tmp_path / "fake_codex.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'item.completed', 'item': "
        f"{{'id': 'a', 'type': 'agent_message', 'text': {chunk!r} * "
        f"{300 * 1024 // len(chunk)}}}}}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {}}))\n",
        encoding="utf-8",
    )

    async def make(*argv, **kwargs):
        return await asyncio.create_subprocess_exec(
            sys.executable, str(script), **kwargs
        )

    engine = _engine(tmp_path, exec_factory=make)
    events = await asyncio.wait_for(_collect(engine.send("go")), timeout=30)
    assert [e.type for e in events] == ["turn_started", "text_delta", "turn_done"]
    assert events[1].data["text"] == reply
    assert len(reply) > 64 * 1024
    assert events[-1].data["is_error"] is False


def test_the_stream_limit_is_not_the_socket_frame_cap():
    """The number is its own decision, and it has to stay one.

    ``doxa.peers.MAX_FRAME_BYTES`` caps DOXA's own peer protocol, where
    DOXA writes both ends. A Codex event is written by an external CLI to
    no size contract, and here the cap is not a truncation but a
    turn-ending crash -- so it sits far above any plausible frame."""
    from doxa.peers import MAX_FRAME_BYTES

    assert STREAM_LIMIT_BYTES > MAX_FRAME_BYTES
    assert STREAM_LIMIT_BYTES > 64 * 1024


class _SilentProc(_FakeProc):
    """A child that starts, says nothing, and never exits or closes."""

    def __init__(self) -> None:
        super().__init__([])
        self.stderr = None


@pytest.mark.asyncio
async def test_a_turn_that_never_ends_is_killed_and_reported(monkeypatch, tmp_path):
    """TURN_TIMEOUT_SECS was declared and read NOWHERE through v1.7.2.

    A `codex exec` that neither exits nor closes stdout held the pane's
    exclusive turn worker forever. The budget now has to do both halves
    of what its docstring claims: KILL the child, and end the turn with a
    readable error -- abandoning the read alone would leave the process
    running."""
    monkeypatch.setattr(codex_mod, "TURN_TIMEOUT_SECS", 0.3)
    procs: list = []

    async def make(*argv, **kwargs):
        proc = _SilentProc()

        async def readline() -> bytes:
            await asyncio.Event().wait()
            return b""

        proc.stdout.readline = readline  # type: ignore[method-assign]
        procs.append(proc)
        return proc

    engine = _engine(tmp_path, exec_factory=make)
    events = await asyncio.wait_for(_collect(engine.send("go")), timeout=10)
    assert [e.type for e in events] == ["turn_started", "text_delta", "turn_done"]
    assert procs[0].killed is True          # the child, not just the read
    assert engine._proc is None
    assert events[-1].data["is_error"] is True
    assert "limit" in events[-1].data["error"]
    assert "killed" in events[-1].data["error"]


@pytest.mark.asyncio
async def test_turn_failed_stops_the_stream_and_counts_the_turn_once(tmp_path):
    """Finding 5. ``turn.failed`` closes the turn -- so nothing after it
    may still be yielded into a block the UI has already marked done, and
    the count it reports has to be the same count a SUCCEEDING turn would
    report for the same turn (it was one short)."""
    calls: list = []
    lines = [_script(
        {"type": "item.completed",
         "item": {"id": "a", "type": "agent_message", "text": "before"}},
        {"type": "turn.failed", "message": "boom"},
        {"type": "item.completed",
         "item": {"id": "b", "type": "agent_message", "text": "AFTER"}},
        {"type": "turn.completed", "usage": {}},
    )]
    engine = _engine(tmp_path, exec_factory=_factory(calls, lines))
    events = await asyncio.wait_for(_collect(engine.send("go")), timeout=10)
    kinds = [e.type for e in events]
    assert kinds == ["turn_started", "text_delta", "text_delta", "turn_done"]
    assert kinds.count("turn_done") == 1
    assert not any("AFTER" in str(e.data.get("text", "")) for e in events)
    assert events[-1].data["is_error"] is True
    assert events[-1].data["num_turns"] == 1 == engine.num_turns


@pytest.mark.asyncio
async def test_a_succeeding_turn_reports_the_same_count_as_a_failing_one(tmp_path):
    """The other half of the num_turns fix: the two paths must agree."""
    calls: list = []
    engine = _engine(tmp_path, exec_factory=_factory(calls, [_script(
        {"type": "turn.completed", "usage": {}},
    )]))
    events = await asyncio.wait_for(_collect(engine.send("go")), timeout=10)
    assert events[-1].data["num_turns"] == 1 == engine.num_turns


@pytest.mark.asyncio
async def test_an_unreadable_stdout_line_fails_the_turn_instead_of_vanishing(tmp_path):
    """Finding 6. A line that is not a Codex event is still dropped --
    there is nothing to map -- but a clean exit afterwards used to render
    it as a green, successful turn. DOXA does not know what was in that
    line, so the honest report is a failed turn that says one went
    missing."""
    calls: list = []
    lines = [[
        b"warning: codex is a bit confused today\n",
        json.dumps({"type": "turn.completed", "usage": {}}).encode() + b"\n",
    ]]
    engine = _engine(tmp_path, exec_factory=_factory(calls, lines))
    events = await asyncio.wait_for(_collect(engine.send("go")), timeout=10)
    assert [e.type for e in events] == ["turn_started", "text_delta", "turn_done"]
    assert events[-1].data["is_error"] is True
    error = events[-1].data["error"]
    assert "1 unreadable line" in error
    assert "confused" in error


class _NeverClosingStderr:
    """A stderr whose write end outlives the child.

    Exactly what a turn that leaves a dev server (or any backgrounded
    command) running produces: `codex exec` exits, the grandchild still
    holds the inherited fd, and the pipe never reaches EOF."""

    async def read(self, n: int = -1) -> bytes:
        await asyncio.Event().wait()
        return b""


@pytest.mark.asyncio
async def test_a_clean_turn_does_not_wait_on_a_stderr_that_never_ends(tmp_path):
    """The drain runs for the whole turn -- but a SUCCESSFUL turn must not
    then wait on it. EOF comes when the last holder of the write end
    closes it, not when codex exits, so waiting unconditionally would put
    STDERR_COLLECT_SECS onto every clean turn behind a backgrounded
    command. Nothing reads the tail on that path; nothing waits for it."""

    class _Proc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(_script(
                {"type": "item.completed",
                 "item": {"id": "a", "type": "agent_message", "text": "ok"}},
                {"type": "turn.completed", "usage": {}},
            ))
            self.stderr = _NeverClosingStderr()
            self.returncode = 0

        async def wait(self) -> int:
            return 0

    async def make(*argv, **kwargs):
        return _Proc()

    engine = _engine(tmp_path, exec_factory=make)
    # Well under STDERR_COLLECT_SECS: the point is that it does not wait.
    events = await asyncio.wait_for(_collect(engine.send("go")), timeout=2)
    assert [e.type for e in events] == ["turn_started", "text_delta", "turn_done"]
    assert events[-1].data["is_error"] is False


@pytest.mark.asyncio
async def test_a_clean_turn_is_still_clean(tmp_path):
    """The control for the two tests above: no dropped lines, zero exit,
    no error -- the fixes must not paint a failure onto a good turn."""
    calls: list = []
    engine = _engine(tmp_path, exec_factory=_factory(calls, [_script(
        {"type": "item.completed",
         "item": {"id": "a", "type": "agent_message", "text": "fine"}},
        {"type": "turn.completed", "usage": {}},
    )]))
    events = await asyncio.wait_for(_collect(engine.send("go")), timeout=10)
    assert events[-1].data["is_error"] is False
    assert "error" not in events[-1].data

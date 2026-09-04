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

import pytest

from doxa import diff as diff_mod
from doxa import engines as engines_mod
from doxa.codex import (
    CODEX_CAPABILITIES,
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
    missing = [n for n in Engine.__protocol_attrs__ if not hasattr(client, n)]
    assert missing == []


def test_session_engine_satisfies_the_protocol_unchanged(tmp_path):
    from doxa.engine import SessionEngine

    engine = SessionEngine(cwd=str(tmp_path))
    assert isinstance(engine, Engine)


def test_codex_engine_satisfies_the_protocol(tmp_path):
    assert isinstance(CodexEngine(cwd=str(tmp_path)), Engine)


def test_stop_is_not_in_the_protocol():
    """SessionEngine has no ``stop`` -- it is EngineClient's "finalize the
    daemon NOW" verb. A Protocol carrying it would have been written
    against one implementation instead of the seam, and the pane already
    reaches it through getattr."""
    from doxa.client import EngineClient
    from doxa.engine import SessionEngine

    assert "stop" not in Engine.__protocol_attrs__
    assert hasattr(EngineClient, "stop")
    assert not hasattr(SessionEngine, "stop")


def test_the_two_async_divergent_methods_stayed_out():
    """lore_write_state/belief_action_state are sync on SessionEngine and
    async on EngineClient. One signature cannot be honest about both."""
    for name in ("lore_write_state", "belief_action_state"):
        assert name not in Engine.__protocol_attrs__


# -- the registry ------------------------------------------------------


def test_registry_closure():
    """Every engine, listed literally -- adding one is a reviewed act."""
    assert engines_mod.available() == (CLAUDE_ENGINE_ID, CODEX_ENGINE_ID)


def test_unknown_engine_raises_and_lists_the_real_ones():
    with pytest.raises(KeyError) as excinfo:
        engines_mod.get("gpt-9")
    message = excinfo.value.args[0]
    assert "gpt-9" in message
    assert "claude" in message and "codex" in message


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
    assert caps.mcp_tools is False        # verified reachable, not taken
    assert caps.permission_modes is False
    assert caps.tool_gate is False
    assert caps.detachable is False
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
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


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


def test_cli_engine_codex_runs_in_process_with_a_codex_factory(monkeypatch, tmp_path):
    """No daemon hosts a Codex session -- doxa.daemon's RPC surface is
    SessionEngine's -- so the flag takes the in-process door and the
    factory it installs builds a CodexEngine."""
    from doxa import cli as cli_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_mod, "DoxaApp", _RecordingApp)
    _RecordingApp.last = None
    assert cli_mod.main(["--engine", "codex"]) == 0
    kwargs = _RecordingApp.last
    assert kwargs is not None
    engine = kwargs["engine_factory"]()
    assert isinstance(engine, CodexEngine)
    # And every other door the app has onto a new session is the same
    # engine -- a Ctrl+T tab on a Codex window must not open a Claude one.
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

"""SDK transport replacement tests, no provider subprocess or real credentials."""
import uuid
import pytest
from doxa.engine import SessionEngine


class Client:
    def __init__(self, options, identity, fail=False):
        self.options, self.identity, self.fail = options, identity, fail
        self.closed = False
    async def __aenter__(self):
        if self.fail:
            raise RuntimeError("fake connect failed")
        return self
    async def __aexit__(self, *_):
        self.closed = True
    async def get_server_info(self):
        return {"session_id": self.identity} if self.identity else {}


@pytest.mark.asyncio
async def test_effort_resume_preserves_session_and_uses_instance_override(monkeypatch, tmp_path):
    sid = str(uuid.uuid4())
    created = []
    def factory(options):
        client = Client(options, sid)
        created.append(client)
        return client
    engine = SessionEngine(str(tmp_path), model="sonnet", session_id=sid, client_factory=factory, peer_presence=False)
    await engine.start()
    old = engine._client
    engine.num_turns = 1  # provider conversation exists and must be resumed
    original_transcript = engine.transcript_path
    original_peer = engine.peer_host
    assert await engine.set_effort("high") == "high"
    assert old.closed
    assert created[-1].options.resume == sid
    assert created[-1].options.session_id is None
    assert created[-1].options.model == "sonnet"
    assert created[-1].options.effort == "high"
    assert engine.session_id == sid
    assert engine.transcript_path == original_transcript
    assert engine.peer_host is original_peer
    assert engine._resume_identity_pending is None
    assert "DOXA_EFFORT" not in __import__("os").environ


@pytest.mark.asyncio
async def test_effort_pending_identity_and_busy_refusal(tmp_path):
    engine = SessionEngine(str(tmp_path), client_factory=lambda options: Client(options, None), peer_presence=False)
    await engine.start()
    engine._turn_running = True
    with pytest.raises(RuntimeError, match="idle"):
        await engine.set_effort("high")
    engine._turn_running = False
    await engine.set_effort("high")
    assert engine._resume_identity_pending == engine.session_id
    with pytest.raises(ValueError):
        await engine.set_effort("invented")


@pytest.mark.asyncio
async def test_effort_changed_identity_rolls_back_transport_and_preference(tmp_path):
    sid = str(uuid.uuid4())
    identities = iter([sid, str(uuid.uuid4()), sid])
    engine = SessionEngine(str(tmp_path), session_id=sid,
                           client_factory=lambda options: Client(options, next(identities)), peer_presence=False)
    await engine.start()
    previous = engine.effort
    with pytest.raises(RuntimeError, match="previous effort restored"):
        await engine.set_effort("max")
    assert engine._connected
    assert engine._effort_override is None
    assert engine.effort == previous
    assert engine.session_id == sid

@pytest.mark.asyncio
@pytest.mark.parametrize("matching", [True, False])
async def test_first_resumed_turn_verifies_identity_before_output(tmp_path, matching):
    from claude_agent_sdk import SystemMessage, AssistantMessage, TextBlock
    from tests.fakes import FakeClient
    sid = str(uuid.uuid4())
    script = [SystemMessage(subtype="init", data={"session_id": sid if matching else str(uuid.uuid4()), "model":"sonnet"}),
              AssistantMessage(content=[TextBlock(text="safe response")], model="sonnet")]
    created = []
    def factory(options):
        client = FakeClient(options, script=script)
        created.append(client)
        return client
    engine = SessionEngine(str(tmp_path), session_id=sid, client_factory=factory, peer_presence=False)
    await engine.start()
    original = engine.effort
    await engine.set_effort("high")
    assert engine.effort == original
    events = []
    if matching:
        events = [event async for event in engine.send("continue")]
        assert any(event.type == "effort_verified" for event in events)
        assert engine.effort == "high"
    else:
        with pytest.raises(RuntimeError, match="identity"):
            async for event in engine.send("continue"):
                events.append(event)
        assert not any(event.type == "text_delta" for event in events)
        assert not engine._connected
        assert created[-1].exited

@pytest.mark.asyncio
@pytest.mark.parametrize("preamble", ["hooks", "ended", "excess", "wrong-session", "content"])
async def test_resumed_identity_preamble_is_bounded_and_never_releases_unverified_content(tmp_path, preamble):
    from claude_agent_sdk import SystemMessage, AssistantMessage, TextBlock, StreamEvent
    from claude_agent_sdk._internal.message_parser import parse_message
    from tests.fakes import FakeClient
    sid = str(uuid.uuid4())
    hook = parse_message({"type": "system", "subtype": "hook_started", "hook_event": "SessionStart", "session_id": sid})
    quota = parse_message({"type":"rate_limit_event", "rate_limit_info":{"status":"allowed"}, "uuid":"fixture", "session_id":sid})
    init = SystemMessage(subtype="init", data={"session_id": sid, "model": "sonnet"})
    text = AssistantMessage(content=[TextBlock(text="verified reply")], model="sonnet")
    scripts = {
        "hooks": [quota, hook, SystemMessage(subtype="status", data={}), init, StreamEvent(uuid="fixture", session_id=sid, event={"type":"content_block_delta", "delta":{"type":"text_delta", "text":"verified reply"}}), text],
        "ended": [hook], "excess": [hook] * 33 + [init, text],
        "wrong-session": [SystemMessage(subtype="status", data={"session_id": str(uuid.uuid4())}), init, text],
        "content": [text, init],
    }
    engine = SessionEngine(str(tmp_path), session_id=sid, effort="low", lore=False,
        client_factory=lambda opts: FakeClient(opts, script=scripts[preamble]), peer_presence=False)
    await engine.start()
    await engine.set_effort("high")
    events = []
    if preamble == "hooks":
        events = [event async for event in engine.send("continue")]
        assert engine._connected and engine.effort == "high"
        assert [e.type for e in events].index("effort_verified") < [e.type for e in events].index("text_delta")
    else:
        with pytest.raises(RuntimeError, match="identity"):
            async for event in engine.send("continue"):
                events.append(event)
        assert not engine._connected and engine.effort == "low"
        assert engine._resume_identity_pending is None and engine._effort_override == "low"
        assert not any(e.type == "text_delta" for e in events)
        failure = next(e for e in events if e.type == "effort_verification_failed")
        assert failure.data == {"effort": "low", "requested_effort": "high", "session_id": sid}


@pytest.mark.asyncio
async def test_real_sdk_cli_fixture_first_turn_and_effort_reconnect(tmp_path, monkeypatch):
    """Exercise the real SDK transport with an owned, provider-free CLI."""
    import dataclasses
    import sys
    from claude_agent_sdk import ClaudeSDKClient
    cli = tmp_path / "fixture-cli"
    log = tmp_path / "launches.jsonl"
    cli.write_text(f"#!{sys.executable}\n" + r'''
import json, sys
from pathlib import Path
args = sys.argv[1:]
if "--version" in args:
    print("2.1.144 (Claude Code)"); sys.exit()
def arg(name):
    joined = next((value.split("=", 1)[1] for value in args if value.startswith(name+"=")), None)
    return joined if joined is not None else (args[args.index(name)+1] if name in args else None)
sid = arg("--resume") or arg("--session-id")
with Path(__file__).with_name("launches.jsonl").open("a") as out:
    out.write(json.dumps({"session": sid, "effort": arg("--effort"), "resume": arg("--resume")})+"\n")
def emit(data):
    print(json.dumps(data), flush=True)
for line in sys.stdin:
    frame = json.loads(line)
    if frame["type"] == "control_request":
        emit({"type": "control_response", "response": {"subtype": "success", "request_id": frame["request_id"], "response": {}}})
    elif frame["type"] == "user":
        emit({"type":"rate_limit_event", "rate_limit_info":{"status":"allowed"}, "uuid":"fixture", "session_id":sid})
        emit({"type": "system", "subtype": "hook_started", "hook_event": "SessionStart", "session_id": sid})
        emit({"type": "system", "subtype": "status"})
        emit({"type": "system", "subtype": "init", "session_id": sid, "model": "fixture-sonnet"})
        emit({"type": "stream_event", "uuid": "fixture", "session_id": sid, "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "fixture response"}}})
        emit({"type": "assistant", "message": {"model": "fixture-sonnet", "content": [{"type": "text", "text": "fixture response"}]}})
        emit({"type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 0, "is_error": False, "num_turns": 1, "session_id": sid, "total_cost_usd": 0, "usage": {}})
''')
    cli.chmod(0o700)
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    sid = str(uuid.uuid4())
    engine = SessionEngine(str(tmp_path), session_id=sid, model="fixture-sonnet", effort="low",
        lore=False, peer_presence=False,
        client_factory=lambda options: ClaudeSDKClient(dataclasses.replace(options, cli_path=str(cli))))
    try:
        await engine.start()
        first = [e async for e in engine.send("fixture first turn")]
        assert any(e.type == "text_delta" for e in first)
        assert engine.num_turns == 1
        await engine.set_effort("high")
        assert engine.effort == "low" and engine._resume_identity_pending == sid
        second = [e async for e in engine.send("fixture resumed turn")]
        assert any(e.type == "effort_verified" for e in second)
        assert engine.effort == "high" and engine.session_id == sid
        launches = [__import__("json").loads(line) for line in log.read_text().splitlines()]
        assert launches == [{"session": sid, "effort": "low", "resume": None}, {"session": sid, "effort": "high", "resume": sid}]
    finally:
        await engine.finalize()

@pytest.mark.asyncio
async def test_failed_effort_and_failed_rollback_leave_no_pending_identity_or_transport(tmp_path):
    sid = str(uuid.uuid4())
    attempts = iter([False, True, True])
    clients = []
    def factory(options):
        client = Client(options, sid, fail=next(attempts))
        clients.append(client)
        return client
    engine = SessionEngine(str(tmp_path), session_id=sid, effort="low", lore=False,
                           client_factory=factory, peer_presence=False)
    await engine.start()
    with pytest.raises(RuntimeError, match="previous effort restored"):
        await engine.set_effort("high")
    assert engine.effort == "low" and engine._effort_override == "low"
    assert engine._resume_identity_pending is None
    assert not engine._connected and engine._client is None
    assert all(client.closed for client in clients)

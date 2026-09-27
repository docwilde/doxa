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

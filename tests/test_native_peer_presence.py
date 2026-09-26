"""Native registry ownership with real SDK peer delivery and a fake client."""
import asyncio
import json
import os
import uuid

import pytest

from doxa import peers
from doxa.engine import SessionEngine
from tests.fakes import factory_with_script


@pytest.mark.asyncio
async def test_native_presence_survives_python_lifecycle_and_sdk_delivery(tmp_path, monkeypatch):
    runtime = tmp_path.parent / ("np-" + uuid.uuid4().hex[:8])
    runtime.mkdir()
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DOXA_AGENT_PEER_SEND", "1")
    monkeypatch.setenv("DOXA_PEER_INBOUND_TURNS", "")
    received = []

    async def native_inbox(reader, writer):
        raw = await reader.readline()
        if raw:
            received.append(json.loads(raw))
        writer.close()
        await writer.wait_closed()

    # Same line-framed JSON inbox contract advertised by Rust's Registry.
    servers = []
    session_id = str(uuid.uuid4())
    target_id = str(uuid.uuid4())
    registry = peers.registry_dir()
    for sid in (session_id, target_id):
        path = runtime / f"native-{sid[:8]}.sock"
        servers.append(await asyncio.start_unix_server(native_inbox, path=str(path)))
        entry = {"session_id": sid, "pid": os.getpid(), "socket_path": str(path),
                 "daemon_socket": str(tmp_path / f"daemon-{sid[:8]}.sock"),
                 "cwd": str(tmp_path), "repo_root": None, "title": "native",
                 "started_at": peers._iso_now(), "heartbeat_at": peers._iso_now()}
        (registry / f"{sid}.json").write_text(json.dumps(entry))
    native_entry = registry / f"{session_id}.json"
    original = native_entry.read_bytes()
    factory, _clients = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), session_id=session_id,
                           client_factory=factory, lore=False, peer_presence=False)
    try:
        await engine.start()
        host = engine.peer_host
        assert host is not None, engine.peer_error
        assert not host.publish_presence
        assert native_entry.read_bytes() == original
        host.refresh()
        host.set_title("SDK prompt title")
        host.set_model("haiku")
        host.set_client_count(2)
        host.update_usage(123)
        host.refresh()
        assert native_entry.read_bytes() == original
        targets = engine._peer_delivery.addressable_peers()
        assert [peer.session_id for peer in targets] == [target_id]
        await engine._peer_delivery.tool_send({"to": target_id, "body": "native delivery"})
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        assert received[0]["body"] == "native delivery"
        assert received[0]["from_id"] == session_id
        await engine.finalize()
        assert native_entry.read_bytes() == original
        assert not host.socket_path.exists()
    finally:
        if not engine._finalized:
            await engine.finalize()
        for server in servers:
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_native_presence_survives_failed_or_cancelled_peer_start(tmp_path, monkeypatch, failure):
    runtime = tmp_path.parent / ("np-" + uuid.uuid4().hex[:8])
    runtime.mkdir()
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(runtime))
    host = peers.PeerHost(str(uuid.uuid4()), str(tmp_path), publish_presence=False)
    original = b'{"owner":"native"}'
    host.registry_path.write_bytes(original)

    def fail_discovery():
        raise failure()

    monkeypatch.setattr(host, "list_peers", fail_discovery)
    try:
        with pytest.raises(failure):
            await host.start()
    finally:
        await host.stop()
    assert host.registry_path.read_bytes() == original
    assert not host.socket_path.exists()

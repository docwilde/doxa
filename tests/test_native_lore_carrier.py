# SPDX-License-Identifier: AGPL-3.0-only
"""Owned CLI protocol fixtures; no native store, provider or user config."""
import json
import os
from pathlib import Path
import sys
import time

import pytest

from doxa.native_lore import Agent, Carrier, NativeLoreError


def script(tmp_path, behavior):
    path = tmp_path / "lore-rs"
    path.write_text(f"#!{sys.executable}\n" + '''import json, sys, time
agent = sys.argv[1] == "agent-bridge"
caps = ["agent_catalog_v1", "agent_tool_v1", "agent_status_v1"] if agent else ["scrub", "snapshot"]
print(json.dumps({"type":"hello", "proto":1,"capabilities":caps}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
''' + behavior)
    path.chmod(0o700)
    return path


def test_retained_carrier_reuses_owned_native_process_and_scrub_protocol(tmp_path, monkeypatch):
    binary = script(tmp_path, '''    print(json.dumps({"type":"reply","id":request["id"],"ok":True,"text":"[REDACTED]"}), flush=True)
''')
    monkeypatch.setenv("DOXA_LORE_RS", str(binary))
    client = Carrier(timeout=0.5)
    try:
        assert client.request("scrub", text="fake-secret") == "[REDACTED]"
        process = client.process
        assert client.request("snapshot", cwd=str(tmp_path), scope="all") == "[REDACTED]"
        assert client.process is process
    finally:
        client.close()
    assert process.returncode is not None
    assert client.process is None


@pytest.mark.parametrize("reply", [
    '{"type":"reply","id":999,"ok":true,"text":"input-secret"}',
    '{"type":"reply","id":1,"ok":true,"text":42}',
    '{"type":"reply","id":1,"ok":false,"error":"input_secret"}',
])
def test_invalid_native_results_never_echo_payload_or_exception(tmp_path, monkeypatch, reply):
    binary = script(tmp_path, f"    print({reply!r}, flush=True)\n")
    monkeypatch.setenv("DOXA_LORE_RS", str(binary))
    client = Carrier(timeout=0.5)
    try:
        with pytest.raises(NativeLoreError) as error:
            client.request("scrub", text="input-secret")
        assert "input" not in str(error.value)
        assert "secret" not in str(error.value)
    finally:
        client.close()


def test_native_timeout_reaps_owned_process_without_reader_thread(tmp_path, monkeypatch):
    binary = script(tmp_path, "    time.sleep(10)\n")
    monkeypatch.setenv("DOXA_LORE_RS", str(binary))
    client = Carrier(timeout=0.1)
    start = time.monotonic()
    with pytest.raises(NativeLoreError, match="timeout"):
        client.request("scrub", text="x")
    assert client.process is None
    assert time.monotonic() - start < 1


def test_native_frame_budget_refuses_output_before_buffer_growth(tmp_path, monkeypatch):
    binary = script(tmp_path, "    print('x' * 2048, flush=True)\n")
    monkeypatch.setenv("DOXA_LORE_RS", str(binary))
    client = Carrier(timeout=0.5)
    client.limit = 1024
    with pytest.raises(NativeLoreError, match="frame_too_large"):
        client.request("scrub", text="x")
    assert client.process is None and not client.buffer


def test_native_request_does_not_fall_back_when_binary_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_LORE_RS", str(tmp_path / "missing"))
    client = Carrier(timeout=0.1)
    with pytest.raises(NativeLoreError, match="native_lore_unavailable"):
        client.request("scrub", text="fake-secret")
    assert not (tmp_path / "state.db").exists()


def test_agent_rebinds_after_transport_restart_with_same_frozen_host_identity(tmp_path, monkeypatch):
    log = tmp_path / "requests"
    binary = script(tmp_path, f'''    with open({str(log)!r}, "a") as file: file.write(json.dumps(request)+"\\n")
    if request["op"] == "agent_catalog_v1":
        value = [{{"name":"lore_remember", "description":"stage only", "inputSchema":{{"type":"object"}}}}]
    else:
        value = {{"staged":True}}
    print(json.dumps({{"type":"reply","id":request["id"],"ok":True,"value":value}}), flush=True)
''')
    monkeypatch.setenv("DOXA_LORE_RS", str(binary))
    monkeypatch.setenv("LORE_ROOT", str(tmp_path / "lore"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa"))
    agent = Agent(session_id="owned", cwd=str(tmp_path), engine="claude")
    try:
        assert agent.call("lore_remember", {"text":"fixture"}) == {"staged":True}
        agent.carrier.close()
        assert agent.call("lore_remember", {"text":"fixture2"}) == {"staged":True}
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        assert [row["op"] for row in rows] == ["agent_catalog_v1", "agent_tool_v1"] * 2
        assert all(row["identity"] == agent.identity for row in rows)
        assert all(row["identity"]["source_engine"] == "claude" for row in rows)
    finally:
        agent.carrier.close()


@pytest.mark.parametrize("code", ["invalid_request", "over_cap", "unsafe_path", "untrusted_write"])
def test_native_argument_and_policy_refusals_do_not_disable_tools(tmp_path, monkeypatch, code):
    from doxa.gate import OperatorContext, ToolGate
    binary = script(tmp_path, f'''    if request["op"] == "agent_catalog_v1":
        reply = {{"ok":True,"value":[{{"name":"lore_remember","description":"stage only","inputSchema":{{"type":"object"}}}}]}}
    else:
        reply = {{"ok":False,"error":{code!r}}}
    print(json.dumps({{"type":"reply","id":request["id"],**reply}}), flush=True)
''')
    monkeypatch.setenv("DOXA_LORE_RS", str(binary))
    monkeypatch.setenv("LORE_ROOT", str(tmp_path / "lore"))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    agent = Agent(session_id="owned", cwd=str(tmp_path), engine="claude")
    gate = ToolGate(op_ctx=OperatorContext(session_id="owned", cwd=str(tmp_path),
        repo_root=str(tmp_path), native_lore=agent.call))
    try:
        for _ in range(3):
            result = gate.execute("lore_remember", {"text":"fixture", "op_ctx":{"source_engine":"forged"}})
            assert "error" in result and "failed:" not in result["error"]
        assert gate.disabled_tools() == []
        assert agent.carrier.process is not None
    finally:
        agent.carrier.close()


def test_native_agent_status_rejects_unbounded_or_malformed_metadata(tmp_path, monkeypatch):
    binary = script(tmp_path, '''    value = [] if request["op"] == "agent_catalog_v1" else {"belief_count":True,"disabled_tools":[]}
    print(json.dumps({"type":"reply","id":request["id"],"ok":True,"value":value}), flush=True)
''')
    monkeypatch.setenv("DOXA_LORE_RS", str(binary))
    monkeypatch.setenv("LORE_ROOT", str(tmp_path / "lore"))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    agent = Agent(session_id="owned", cwd=str(tmp_path), engine="claude")
    try:
        with pytest.raises(NativeLoreError, match="invalid_native_status"):
            agent.status()
    finally:
        agent.carrier.close()

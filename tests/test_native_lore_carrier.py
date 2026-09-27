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


def test_retained_engine_requires_review_snapshot_and_forwards_only_explicit_tokens(tmp_path, monkeypatch):
    import asyncio
    from doxa import engine as engine_mod
    calls = []
    def invoke(op, **fields):
        calls.append((op, fields))
        if op == "resolve_reviewed_v1":
            return {"status":"refused", "applied":None, "may_have_applied":True, "error":"archive_failed"}
        if op == "belief_action_v1":
            return {"status":"active", "retired":False}
        return []
    monkeypatch.setattr(engine_mod.native_lore_mod, "request", invoke)
    engine = engine_mod.SessionEngine.__new__(engine_mod.SessionEngine)
    engine.lore, engine.cwd = True, str(tmp_path)
    async def run():
        assert "native-review-required" in await engine.approve_pending("one")
        assert "native-review-required" in await engine.reject_pending("one")
        assert "native-review-required" in await engine.record_belief_outcome(1, "confirmed", "fixture")
        assert "native-review-required" in await engine.retract_belief(1)
        assert not calls
        expected = {"sha256":"a" * 64, "inode":17}
        assert "recovery required" in await engine.approve_pending("one", expected)
        assert calls[-1] == ("resolve_reviewed_v1", {"cwd":str(tmp_path),"pid":"one","decision":"approve","expected":expected})
        belief = {"uid":"owned", "subject":"user", "claim_sha256":"b" * 64}
        assert await engine.record_belief_outcome(1, "confirmed", "fixture", belief) is None
        assert calls[-1][1]["expected"] == belief
        engine.lore = False
        before = len(calls)
        assert await engine.list_beliefs() == []
        assert await engine.list_pending() == []
        assert await engine.belief_evidence(1) == []
        assert "memory is off" in await engine.approve_pending("one", expected)
        assert len(calls) == before
    asyncio.run(run())


def test_vendor_projection_uses_frozen_native_catalog_for_memory(tmp_path):
    from doxa.vendors import operator_tools
    class Native:
        def tools(self):
            return [{"name":"lore_memory_list", "description":"canonical fixture schema", "inputSchema":{"type":"object","properties":{"scope":{"enum":["user"]}}}}]
    rows = operator_tools({"native_lore":Native()})
    memory = [row["function"] for row in rows if row["function"]["name"].startswith("lore_")]
    assert memory == [{"name":"lore_memory_list", "description":"canonical fixture schema", "parameters":{"type":"object","properties":{"scope":{"enum":["user"]}}}}]


def test_native_sync_off_and_read_only_probes_never_mint_identity(monkeypatch):
    from doxa import lore_sync
    calls = []
    def invoke(op, **fields):
        calls.append((op, fields))
        return None
    monkeypatch.setattr(lore_sync.native_lore, "request", invoke)
    monkeypatch.delenv("LORE_SYNC_URL", raising=False)
    monkeypatch.delenv("LORE_SYNC_PEER", raising=False)
    lore_sync.invalidate()
    assert lore_sync.machine_id(create=True) is None
    assert lore_sync.read_state() is None
    assert not calls
    assert lore_sync.machine_id() is None
    assert calls == [("sync_machine_v1", {"create":False})]
    assert lore_sync.machine_id() is None
    assert len(calls) == 1
    lore_sync.invalidate()


def test_native_sync_state_validates_canonical_counts_without_sql(monkeypatch):
    from doxa import lore_sync
    monkeypatch.setenv("LORE_SYNC_URL", "https://owned.invalid")
    monkeypatch.delenv("LORE_DISABLE_SYNC", raising=False)
    monkeypatch.setattr(lore_sync.native_lore, "request", lambda *args, **kwargs:
        {"last_pull_age_s":1.5,"unpushed":2,"conflicts":3,"unverified":4})
    assert lore_sync.read_state() == lore_sync.SyncState(1.5, 2, 3, 4)
    monkeypatch.setattr(lore_sync.native_lore, "request", lambda *args, **kwargs:
        {"last_pull_age_s":float("inf"),"unpushed":True,"conflicts":0,"unverified":0})
    assert lore_sync.read_state() is None


def test_retained_native_context_queries_are_scoped_and_memory_off_is_closed(tmp_path, monkeypatch):
    from doxa import engine as engine_mod
    calls = []
    def invoke(op, **fields):
        calls.append((op, fields))
        return "[BELIEF GRAPH] owned fixture" if op == "graph_awareness_v1" else "bounded fixture"
    monkeypatch.setattr(engine_mod.native_lore_mod, "request", invoke)
    monkeypatch.setenv("DOXA_GRAPH_CONTEXT", "1")
    monkeypatch.delenv("LORE_DISABLE_BELIEFS", raising=False)
    engine = engine_mod.SessionEngine.__new__(engine_mod.SessionEngine)
    engine.lore, engine.cwd = True, str(tmp_path)
    assert engine_mod._graph_awareness_block() == "[BELIEF GRAPH] owned fixture"
    assert engine._graph_context_block("owned prompt") == "bounded fixture"
    assert calls == [("graph_awareness_v1", {}),
                     ("graph_context_v1", {"cwd":str(tmp_path), "prompt":"owned prompt"})]
    engine.lore = False
    assert engine._graph_context_block("owned prompt") == ""
    assert engine._consult_note("owned prompt") is None
    assert len(calls) == 2


def test_retained_error_scrub_unavailability_uses_fixed_placeholder(monkeypatch):
    from doxa import errors
    def refuse(_):
        raise NativeLoreError("native_lore_unavailable")
    monkeypatch.setattr(errors, "scrub_secrets", refuse)
    result = errors.scrub("fake-secret-not-for-output")
    assert "fake-secret" not in result
    assert "unavailable" in result


def test_retained_transcript_path_uses_native_identity_and_rejects_path_components(tmp_path, monkeypatch):
    from doxa import transcript
    calls = []
    def identity(op, **fields):
        calls.append((op, fields))
        return {"projects_dir":str(tmp_path / "projects"),"slug":"canonical-project"}
    monkeypatch.setattr(transcript.native_lore, "request", identity)
    assert transcript.transcript_path("owned-1", str(tmp_path)) == tmp_path / "projects/canonical-project/owned-1.jsonl"
    assert calls == [("transcript_identity", {"cwd":str(tmp_path)})]
    assert transcript.transcript_path("../unsafe", str(tmp_path)) is None
    assert len(calls) == 1
    monkeypatch.setattr(transcript.native_lore, "request", lambda *args, **kwargs:
        {"projects_dir":str(tmp_path / "projects"),"slug":"../outside"})
    assert transcript.transcript_path("owned-1", str(tmp_path)) is None


def test_legacy_operator_helpers_require_frozen_native_context_and_never_fall_back(tmp_path):
    from doxa import operators
    from doxa.gate import OperatorContext
    invocations = [lambda ctx: operators._belief_search("owned", op_ctx=ctx),
        lambda ctx: operators._belief_show(1, op_ctx=ctx),
        lambda ctx: operators._belief_neighbours(1, op_ctx=ctx),
        lambda ctx: operators._memory_list(op_ctx=ctx),
        lambda ctx: operators._session_search("owned", op_ctx=ctx),
        lambda ctx: operators._remember("owned", op_ctx=ctx)]
    for invoke in invocations:
        assert "native session context required" in invoke(None)["error"]
    calls = []
    def native(name, arguments):
        calls.append((name, arguments))
        return {"native":True}
    ctx = OperatorContext(session_id="owned", cwd=str(tmp_path), repo_root=str(tmp_path), native_lore=native)
    for invoke in invocations:
        assert invoke(ctx) == {"native":True}
    assert len(calls) == 6
    assert all("op_ctx" not in arguments for _, arguments in calls)
    assert not (tmp_path / "state.db").exists()


def test_retained_history_uses_bounded_native_readers_and_metadata(tmp_path, monkeypatch):
    from doxa import history
    calls = []
    def request(op, **fields):
        calls.append((op, fields))
        if op == "session_meta_v1":
            return [{"session_id":"owned", "title":"native title", "cwd":str(tmp_path), "engine":"claude"}]
        return [{"session_id":"owned", "snippet":"matched"}]
    monkeypatch.setattr("doxa.native_lore.request", request)
    hits = history.search_sessions("owned", str(tmp_path))
    assert hits[0]["title"] == "native title" and hits[0]["engine"] == "claude"
    history.recent_sessions(str(tmp_path), 100000)
    history.sessions_by_prefix("owned", 100000)
    assert calls == [("session_search_v1", {"cwd":str(tmp_path),"query":"owned"}),
        ("session_meta_v1", {"ids":["owned"]}),
        ("sessions_recent_v1", {"cwd":str(tmp_path),"limit":20}),
        ("sessions_prefix_v1", {"prefix":"owned","limit":9})]
    def refuse(*args, **fields):
        raise NativeLoreError("native_lore_unavailable")
    monkeypatch.setattr("doxa.native_lore.request", refuse)
    assert history.recent_sessions(str(tmp_path)) == []
    assert history.with_titles([{"session_id":"owned"}]) == [{"session_id":"owned"}]
    assert not (tmp_path / "state.db").exists()


def test_retained_history_artifact_scan_is_native_owned_and_finite(tmp_path, monkeypatch):
    from doxa import history
    root = tmp_path / "projects"
    (root / "a").mkdir(parents=True)
    artifact = root / "a/owned.codex.json"
    artifact.write_text('{}')
    (root / "b").symlink_to(root / "a", target_is_directory=True)
    monkeypatch.setattr("doxa.native_lore.request", lambda *args, **kwargs:{"projects_dir":str(root)})
    assert history._beside_transcript("owned", ".codex.json") == [artifact]
    assert history._beside_transcript("../escape", ".codex.json") == []
    assert history._beside_transcript("owned", "../bad") == []
    class Entry:
        def is_dir(self, **kwargs): return False
    class Overflow:
        def __enter__(self): return iter([Entry()]*4097)
        def __exit__(self, *args): pass
    monkeypatch.setattr(history.os, "scandir", lambda *args:Overflow())
    assert history._beside_transcript("owned", ".codex.json") == []

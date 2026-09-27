# SPDX-License-Identifier: AGPL-3.0-only
"""Actual compiled native CLI with owned roots; never a provider or user store."""
import json
import os
from pathlib import Path

import pytest

from doxa.native_lore import Agent, Carrier


@pytest.fixture
def native(tmp_path, monkeypatch):
    binary = os.environ.get("DOXA_TEST_LORE_RS", "")
    if not binary:
        pytest.skip("set DOXA_TEST_LORE_RS to the compiled canonical CLI")
    assert Path(binary).is_file()
    for name, value in {"DOXA_LORE_RS":binary,"HOME":str(tmp_path),
            "DOXA_HOME":str(tmp_path / "doxa"),"CLAUDE_CONFIG_DIR":str(tmp_path / "claude"),
            "LORE_ROOT":str(tmp_path / "lore"),"LORE_PROJECTS_DIR":str(tmp_path / "projects"),
            "LORE_CODEX_SESSIONS_DIR":str(tmp_path / "codex"),"LORE_DISABLE_SYNC":"1"}.items():
        monkeypatch.setenv(name, value)
    return tmp_path


def test_native_cli_scrub_is_lazy_across_retained_python_transport(native):
    client = Carrier()
    try:
        assert client.request("scrub", text="owned ordinary fixture") == "owned ordinary fixture"
        assert not (native / "lore").exists()
    finally:
        client.close()


def test_native_cli_claude_agent_stages_only_and_recovers_invalid_arguments(native):
    agent = Agent(session_id="owned-claude", cwd=str(native), engine="claude")
    try:
        assert "lore_remember" in {row["name"] for row in agent.tools()}
        for _ in range(3):
            refused = agent.call("lore_remember", {"text":"owned proposal", "authority":"human"})
            assert "error" in refused
        assert agent.status()["disabled_tools"] == []
        staged = agent.call("lore_remember", {"text":"owned proposal", "scope":"user"})
        pid = staged["staged"]
        assert isinstance(pid, str) and pid
        record = json.loads((native / "lore/pending" / f"{pid}.json").read_text())
        assert record["text"] == "owned proposal"
        assert record["source_engine"] == "claude"
        assert not (native / "lore/USER.md").exists()
        assert not (native / "lore/MEMORY.md").exists()
    finally:
        agent.carrier.close()


def test_retained_native_config_and_memory_off_hosts_never_open_store(native, monkeypatch):
    from doxa import native_lore, engine, codex, vendors
    native_lore._default.close()
    try:
        config = native_lore.runtime_config()
        assert config["root"] == str(native / "lore")
        assert config["projects_dir"] == str(native / "projects")
        assert isinstance(config["disabled_stages"], list)
        handles = [engine.SessionEngine(str(native), session_id="owned-claude", lore=False, peer_presence=False),
            codex.CodexEngine(str(native), session_id="owned-codex", lore=False, account_fetch=lambda:{}),
            vendors.ChatApiEngine(str(native), session_id="owned-vendor", lore=False)]
        for handle in handles:
            assert handle._projects_dir == native / "projects"
            assert not handle.lore
        assert not (native / "lore").exists()
    finally:
        native_lore._default.close()


def test_retained_history_reads_actual_native_index_without_python_backend(native):
    from doxa import native_lore, history
    native_lore._default.close()
    try:
        directory, slug = native_lore.transcript_identity(str(native))
        directory = directory / slug
        directory.mkdir(parents=True)
        record = {"type":"user","cwd":str(native),"timestamp":"2026-09-27T10:00:00Z",
            "message":{"content":"owned history keyword"}}
        (directory / "owned-history.jsonl").write_text(json.dumps(record) + "\n")
        result = native_lore.request("index_transcript_v1", cwd=str(native), session_id="owned-history")
        assert result == {"indexed":1,"consumed":1}
        rows = history.recent_sessions(str(native), 1)
        assert rows[0]["session_id"] == "owned-history" and rows[0]["messages"] == 1
        assert history.sessions_by_prefix("owned-h")[0]["session_id"] == "owned-history"
        hits = history.search_sessions("keyword", str(native))
        assert hits[0]["session_id"] == "owned-history"
        assert hits[0]["cwd"] == str(native) and hits[0]["engine"] == "claude"
    finally:
        native_lore._default.close()

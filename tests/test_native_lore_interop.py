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

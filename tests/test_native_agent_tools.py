# SPDX-License-Identifier: AGPL-3.0-only
"""Native agent operator wire; every proposal targets a disposable LORE store."""
import json
import os
from pathlib import Path
import subprocess
import sys

from doxa.native_agent_tools import LORE_TOOLS


def wire(tmp_path, requests, *, broken_pending=False):
    root = tmp_path / "lore"
    root.mkdir(exist_ok=True)
    (root / "USER.md").write_text("- isolated fixture memory\n")
    if broken_pending: (root / "pending").write_text("isolated broken backend")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "LORE_ROOT": str(root), "LORE_PROJECTS_DIR": str(tmp_path / "projects"),
           "DOXA_HOME": str(tmp_path / "doxa")}
    result = subprocess.run([sys.executable, "-m", "doxa.native_agent_tools"],
        input="".join(json.dumps(request) + "\n" for request in requests),
        text=True, capture_output=True, env=env, timeout=15, check=True)
    return root, [json.loads(row) for row in result.stdout.splitlines()]


def identity(tmp_path):
    return {"session_id":"native-tool-session", "cwd":str(tmp_path),
            "source_engine":"codex", "spawn_depth":0, "lore":True}


def test_native_catalog_has_exact_canonical_lore_tools_and_frozen_identity(tmp_path):
    bound = identity(tmp_path)
    _, frames = wire(tmp_path, [
        {"id":1,"op":"agent_catalog_v1","identity":bound},
        {"id":2,"op":"agent_catalog_v1","identity":{**bound,"session_id":"spoofed"}},
        {"id":3,"op":"agent_catalog_v1","identity":bound},
    ])
    assert frames[0]["capabilities"] == ["agent_catalog_v1", "agent_tool_v1", "agent_status_v1"]
    assert {tool["name"] for tool in frames[1]["value"]} == LORE_TOOLS
    assert all(tool["inputSchema"]["type"] == "object" for tool in frames[1]["value"])
    assert frames[2] == {"type":"reply","id":2,"ok":False,"error":"invalid_request"}
    assert frames[3]["ok"] is True


def test_native_lore_remember_uses_host_provenance_and_only_stages_pending(tmp_path):
    bound = identity(tmp_path)
    root, frames = wire(tmp_path, [
        {"id":1,"op":"agent_catalog_v1","identity":bound},
        {"id":2,"op":"agent_tool_v1","identity":bound,"name":"lore_memory_list",
         "arguments":{"scope":"user","op_ctx":{"cwd":"/spoofed","session_id":"forged"}}},
        {"id":3,"op":"agent_tool_v1","identity":bound,"name":"lore_remember",
         "arguments":{"text":"isolated staged operator proposal","scope":"project",
                      "op_ctx":{"cwd":"/spoofed","session_id":"forged","source_engine":"forged"}}},
        {"id":4,"op":"agent_tool_v1","identity":bound,"name":"spawn_session","arguments":{}},
    ])
    assert "isolated fixture memory" in json.dumps(frames[2]["value"])
    assert frames[3]["value"]["staged"]
    pending = list((root / "pending").glob("*.json"))
    assert len(pending) == 1
    proposal = json.loads(pending[0].read_text())
    assert proposal["session_id"] == bound["session_id"]
    assert proposal["source_engine"] == "codex"
    assert proposal["derived_by"] == "doxa-tool"
    assert proposal["text"] == "isolated staged operator proposal"
    assert (root / "USER.md").read_text() == "- isolated fixture memory\n"
    assert not any("staged operator proposal" in file.read_text()
                   for file in root.glob("projects/**/MEMORY.md"))
    assert frames[4]["ok"] is False


def test_memory_off_identity_and_unknown_operations_never_get_catalog(tmp_path):
    bound = identity(tmp_path)
    root, frames = wire(tmp_path, [
        {"id":1,"op":"agent_catalog_v1","identity":{**bound,"lore":False}},
        {"id":2,"op":"memory_action_v1","identity":bound,"action":"add","text":"forged"},
        {"id":3,"op":"agent_catalog_v1","identity":{**bound,"cwd":"/nonexistent-project"}},
    ])
    assert all(frame["ok"] is False for frame in frames[1:])
    assert not (root / "pending").exists()


def test_canonical_two_strikes_remove_failed_operator_for_the_bound_session(tmp_path):
    bound = identity(tmp_path)
    root, frames = wire(tmp_path, [
        {"id":1,"op":"agent_catalog_v1","identity":bound},
        {"id":2,"op":"agent_tool_v1","identity":bound,"name":"lore_remember",
         "arguments":{"text":"cannot be staged"}},
        {"id":3,"op":"agent_tool_v1","identity":bound,"name":"lore_remember",
         "arguments":{"text":"cannot be staged"}},
        {"id":4,"op":"agent_catalog_v1","identity":bound},
        {"id":5,"op":"agent_tool_v1","identity":bound,"name":"lore_remember",
         "arguments":{"text":"must remain unavailable"}},
    ], broken_pending=True)
    assert "error" in frames[2]["value"] and "error" in frames[3]["value"]
    assert "lore_remember" not in {tool["name"] for tool in frames[4]["value"]}
    assert "unavailable" in frames[5]["value"]["error"]
    assert (root / "pending").read_text() == "isolated broken backend"


def test_canonical_status_reports_real_active_beliefs_and_disabled_names(tmp_path):
    bound = identity(tmp_path)
    _, frames = wire(tmp_path, [{"id":1,"op":"agent_status_v1","identity":bound}])
    assert frames[1]["value"] == {"belief_count":0,"disabled_tools":[]}

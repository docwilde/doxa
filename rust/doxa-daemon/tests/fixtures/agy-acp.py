#!/usr/bin/python3
"""Deterministic Antigravity ACP fixture with no network or account access."""
import json
import os
from pathlib import Path
import sys

assert sys.argv[1:] == [
    "acp", "--sandbox", "--disable-telemetry", "--include-default-customizations=false"
]
for name in ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "AGY_ADC_AUTH"]:
    assert name not in os.environ
assert os.environ.get("LORE_TOOL_EVIDENCE") == "0"
cwd = str(Path.cwd())
session = "fixture_native_acp_conversation"
model = "fixture-default"
effort = "low"
prompt_id = None
cancel = False


def send(**message):
    print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)


def config():
    return {"configOptions": [
        {"id": "model-choice", "category": "model", "type": "select", "currentValue": model,
         "options": [{"value": "fixture-default", "name": "Default"},
                     {"group": "Gemini", "name": "Gemini", "options":
                         [{"value": "fixture-neon", "name": "Gemini 4 Neon"}]}]},
        {"id": "reasoning", "category": "thought_level", "type": "select", "currentValue": effort,
         "options": [{"value": "low", "name": "Low"}, {"value": "high", "name": "High"}]},
    ]}


def update(**data):
    send(method="session/update", params={"sessionId": session, "update": data})


def text(value):
    update(sessionUpdate="agent_message_chunk", content={"type": "text", "text": value})


for line in sys.stdin:
    message = json.loads(line)
    with Path("fixture-acp.jsonl").open("a") as log:
        log.write(json.dumps(message) + "\n")
    method = message.get("method")
    params = message.get("params", {})
    if method == "initialize":
        assert params["clientCapabilities"] == {}
        result = {"protocolVersion": 1, "agentInfo": {"name": "Antigravity ACP Server", "version": "fixture"},
                  "agentCapabilities": {"sessionCapabilities": {"resume": {}, "close": {}}}}
    elif method in ["session/new", "session/resume"]:
        assert params["cwd"] == cwd and params["mcpServers"] == []
        assert params.get("sessionId") == (session if method == "session/resume" else None)
        result = {"sessionId": session, **config()}
    elif method == "session/set_config_option":
        assert params["sessionId"] == session
        if params["configId"] == "model-choice":
            model = params["value"]
        elif params["configId"] == "reasoning":
            effort = params["value"]
        else:
            raise AssertionError(params)
        result = config()
    elif method == "session/prompt":
        assert params["sessionId"] == session and model == "fixture-neon"
        prompt_id = message["id"]
        prompt = params["prompt"][-1]["text"]
        cancel = prompt == "please cancel"
        fail_tool = prompt == "show tool failure"
        text("Fixture answer sk-abcdefghijk")
        text("lmnopqrstuvwxyz123456 done ")
        tool = {"toolCallId": "tool-1", "title": "Inspect fixture", "rawInput": {"command": "printf test"}}
        update(sessionUpdate="tool_call", **tool)
        options = [{"kind": "allow_once", "optionId": "allow-this"},
                   {"kind": "reject_once", "optionId": "deny-this"},
                   {"kind": "allow_always", "optionId": "always"}]
        send(id="wrong-session", method="session/request_permission",
             params={"sessionId": "someone-else", "toolCall": tool, "options": options})
        send(id="permission", method="session/request_permission",
             params={"sessionId": session, "toolCall": tool, "options": options})
        continue
    elif method == "session/cancel":
        send(id=prompt_id, result={"stopReason": "cancelled"})
        continue
    elif method == "session/close":
        assert params["sessionId"] == session
        result = {}
    elif message["id"] == "wrong-session":
        assert message["result"] == {"outcome": {"outcome": "cancelled"}}
        continue
    elif message["id"] == "permission":
        outcome = message["result"]["outcome"]
        if cancel:
            assert outcome == {"outcome": "cancelled"}
        else:
            assert outcome["optionId"] in ["allow-this", "deny-this"]
            update(sessionUpdate="tool_call_update", toolCallId="tool-1", status="failed" if fail_tool else "completed")
            send(id=prompt_id, result={"stopReason": "end_turn"})
        continue
    else:
        raise AssertionError(message)
    send(id=message["id"], result=result)

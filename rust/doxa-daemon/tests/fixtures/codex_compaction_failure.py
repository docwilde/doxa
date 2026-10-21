#!/usr/bin/env python3
"""Local protocol-only fixture: a clean turn followed by unsettled compaction.

No hook command, reviewer, provider or network request is executed here.
"""
import json
import sys
import tomllib
from pathlib import Path

root = Path(__file__).parent
mode = root.joinpath("compaction-mode").read_text()


def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    with root.joinpath("requests.jsonl").open("a") as log:
        log.write(line)
    return json.loads(line)


def send(value):
    print(json.dumps(value), flush=True)


def notice(method, **params):
    send({"method": method, "params": dict(threadId="thread-1", turnId=turn, **params)})


config = {}


def merge(target, incoming):
    for key, value in incoming.items():
        if isinstance(value, dict):
            merge(target.setdefault(key, {}), value)
        else:
            target[key] = value


for i, item in enumerate(sys.argv[:-1]):
    if item == "-c":
        merge(config, tomllib.loads(sys.argv[i + 1]))
assert config["features"]["token_budget"] is False
assert config["features"]["async_compaction"] is False
assert config["features"]["multi_agent_v2"] is False
assert config["agents"]["enabled"] is False
request = read()
assert request["method"] == "initialize"
send({"id": request["id"], "result": {"userAgent": "doxa_codex_rs/__DOXA_SUPPORTED_CODEX_VERSION__ (doxa-precompact-fail-closed-v1; upstream fixture)"}})
assert read()["method"] == "initialized"
request = read()
if request["method"] == "model/list":
    send({"id": request["id"], "result": {"data": [{"model": "gpt-test", "hidden": False, "isDefault": True, "supportedReasoningEfforts": []}], "nextCursor": None}})
    sys.exit(0)
assert request["method"] == "config/read"
send({"id": request["id"], "result": {"config": config, "origins": {}, "layers": None}})
request = read()
assert request["method"] == "hooks/list"
hooks = config["hooks"]
key = next(iter(hooks["state"]))
row = {"key": key, "command": hooks["PreCompact"][0]["hooks"][0]["command"], "handlerType": "command", "enabled": True, "trustStatus": "trusted", "currentHash": hooks["state"][key]["trusted_hash"], "eventName": "preCompact", "source": "sessionFlags", "timeoutSec": 240, "async": False}
send({"id": request["id"], "result": {"data": [{"hooks": [row]}], "errors": []}})
request = read()
assert request["method"] == "thread/start"
send({"id": request["id"], "result": {"thread": {"id": "thread-1"}, "model": "gpt-test"}})
count = 0
while True:
    request = read()
    if request["method"] == "account/rateLimits/read":
        send({"id": request["id"], "result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20, "windowDurationMins": 300}}}})
        continue
    assert request["method"] == "turn/start"
    count += 1
    turn = f"turn-{count}"
    send({"id": request["id"], "result": {"turn": {"id": turn}}})
    if count == 1:
        notice("thread/tokenUsage/updated", tokenUsage={"last": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}, "total": {"inputTokens": 0, "outputTokens": 0, "cachedInputTokens": 0}, "modelContextWindow": 32000})
        notice("turn/completed", turn={"id": turn, "status": "completed", "error": None})
    elif mode == "unreviewed":
        notice("item/completed", item={"id": "compact-1", "type": "contextCompaction"})
    else:
        run = {"eventName": "preCompact", "source": "sessionFlags", "sourcePath": "/<session-flags>/config.toml", "handlerType": "command", "executionMode": "sync", "status": "failed" if mode == "failed" else "blocked"}
        notice("hook/started", run=run)
        notice("hook/completed", run=run)
    # On the second turn deliberately withhold the terminal and usage. The
    # host must stop this process, preserve the dirty guard and never retry.

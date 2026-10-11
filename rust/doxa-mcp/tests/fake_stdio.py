"""Local synthetic MCP fixture. No imports, keys or calls from a user store."""
import json
import os
import signal
import sys
import time

mode, journal = sys.argv[1:]


def record(value):
    with open(journal, "a", encoding="utf-8") as output:
        output.write(json.dumps(value) + "\n")


def receive():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    value = json.loads(line)
    record({"method": value.get("method"), "id": value.get("id")})
    return value


def reply(request, result):
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


def tool(name):
    return {"name": name, "description": "Synthetic local tool", "inputSchema": {"type": "object"}}


record({"pid": os.getpid(), "unrelated_env_present": "DOXA_MCP_UNRELATED" in os.environ})
request = receive()
assert request["method"] == "initialize" and request["params"]["capabilities"] == {}
if mode == "init_timeout":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        receive()
if mode == "stdout_noise":
    print("server banner is not MCP", flush=True)
else:
    result = {"protocolVersion": "2026-07-28" if mode == "unsupported" else "2025-06-18",
              "capabilities": {"tools": {}}, "serverInfo": {"name": "local-fixture", "version": "1"}}
    if mode == "missing_capability":
        result["capabilities"] = {}
    if mode == "mismatched_id":
        request["id"] += 100
    if mode == "oversize":
        result["instructions"] = "x" * 5000
    if mode == "duplicate_json":
        print('{"jsonrpc":"2.0","id":1,"id":1,"result":{}}', flush=True)
    else:
        reply(request, result)
assert receive()["method"] == "notifications/initialized"
page = 0
while True:
    request = receive()
    if request["method"] == "tools/list":
        if mode == "list_changed" and page == 0:
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}), flush=True)
        if mode == "messages":
            print(json.dumps({"jsonrpc": "2.0", "id": "server-ping", "method": "ping"}), flush=True)
            assert receive()["result"] == {}
            print(json.dumps({"jsonrpc": "2.0", "id": "sampling-request", "method": "sampling/createMessage", "params": {}}), flush=True)
            assert receive()["error"]["code"] == -32601
        if mode == "flood":
            for _ in range(8):
                print(json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}), flush=True)
        if page == 0:
            rows = [tool("search"), tool("admin")]
            if mode == "bad_schema":
                rows[0]["inputSchema"] = []
            if mode == "duplicate_tool":
                rows.append(tool("search"))
            reply(request, {"tools": rows, "nextCursor": "page-two"})
        else:
            assert request["params"]["cursor"] == "page-two"
            result = {"tools": [tool("lookup")]}
            if mode == "cursor_cycle":
                result["nextCursor"] = "page-two"
            reply(request, result)
        page += 1
    elif request["method"] == "tools/call":
        if mode == "timeout":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            child = os.fork()
            if child == 0:
                while True:
                    time.sleep(1)
            record({"descendant": child})
            while True:
                receive()
        elif mode == "rpc_error":
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {
                "code": -32042, "message": os.environ.get("TEST_SECRET", "synthetic")}}), flush=True)
        else:
            if mode == "slow_call":
                # This mode has one caller: a 2 s handshake / 6 s call regression.
                # Exceed the former with room for scheduling inside the latter.
                time.sleep(3.0)
            secret = os.environ.get("TEST_SECRET", "synthetic")
            reply(request, {"content": [{"type": "text", "text": "Local result " + secret}],
                            "structuredContent": {"source": secret}, "isError": mode == "tool_error"})

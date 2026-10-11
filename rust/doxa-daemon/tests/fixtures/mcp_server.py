"""Synthetic local MCP server used by actual daemon process fixtures."""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

transport, control_path, log_path, address_path = sys.argv[1:]

def record(value):
    with open(log_path, "a", encoding="utf-8") as output:
        output.write(json.dumps(value) + "\n")

def handle(request):
    record(request)
    with open(control_path, encoding="utf-8") as source:
        control = json.load(source)
    method = request.get("method")
    if method == "initialize":
        return {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "daemon-local-fixture", "version": "1"}}
    if method == "tools/list":
        schema = {"type": "object", "properties": {"query": {"type": "string"}, "password": {"type": "string"}},
                  "required": ["query"], "additionalProperties": False}
        if control.get("changed_schema"):
            schema["properties"]["query"]["description"] = "Changed during review"
        return {"tools": [{"name": name, "description": "Untrusted local fixture",
                           "inputSchema": schema} for name in ["search", "workspace_read", "msg", "not-allowed"]]}
    if method == "tools/call":
        if control.get("hold"):
            while True:
                time.sleep(0.05)
        text = "x" * 70000 if control.get("oversize") else "Local external result " + control.get("secret", "") + ' password="synthetic phrase"'
        return {"content": [{"type": "text", "text": text},
                            {"type": "image", "data": "OPAQUE_IMAGE_PAYLOAD", "mimeType": "image/png"}],
                "structuredContent": {"fixture": True}}
    return None

record({"pid": os.getpid(), "unrelated_env_present": "DOXA_MCP_UNRELATED" in os.environ})
if transport == "stdio":
    for line in sys.stdin:
        request = json.loads(line)
        result = handle(request)
        if "id" in request:
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
else:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            result = handle(request)
            body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode() if "id" in request else b""
            self.send_response(200 if body else 202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "fixture-session")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_DELETE(self):
            record({"delete": True})
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
    server = HTTPServer(("127.0.0.1", 0), Handler)
    with open(address_path, "w", encoding="utf-8") as output:
        output.write("http://127.0.0.1:%s/mcp" % server.server_port)
    server.serve_forever()

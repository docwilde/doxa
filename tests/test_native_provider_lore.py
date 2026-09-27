# SPDX-License-Identifier: AGPL-3.0-only
"""Native provider callbacks against canonical LORE in disposable homes.

Set DOXA_NATIVE_DAEMON to a native daemon built with local-test-server. Every
HTTP response is local; no provider credentials or real memory are accessed.
"""
import contextlib
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import tempfile
import time

import pytest

from doxa.native_agent_tools import LORE_TOOLS
from doxa.native_lore import executable

BINARY = os.environ.get("DOXA_NATIVE_DAEMON")
pytestmark = pytest.mark.skipif(not BINARY, reason="requires DOXA_NATIVE_DAEMON fixture binary")


@pytest.fixture
def native_home():
    root = Path(os.environ.get("TMPDIR", str(Path.home() / ".cache/doxa-native-lore-tests")))
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nl-", dir=root) as home:
        yield Path(home)


def fixture_env(home, broken_pending=False, peer_enabled=False):
    root = home / "lore"
    root.mkdir()
    (root / "USER.md").write_text("- isolated native user memory\n")
    if broken_pending: (root / "pending").write_text("isolated broken backend")
    return {"HOME":str(home), "PATH":os.environ.get("PATH", ""),
        "PYTHONPATH":str(Path(__file__).resolve().parents[1]),
        "LORE_ROOT":str(root), "LORE_PROJECTS_DIR":str(home / "projects"),
        "DOXA_HOME":str(home / "doxa"), "CODEX_HOME":str(home / "codex"),
        "DOXA_LORE_RS":executable(),
        "DOXA_AGENT_PEER_SEND":"1" if peer_enabled else "0", "DOXA_VENDOR_TOOLS":"", "DEEPSEEK_API_KEY":"isolated-local-fixture"}


@contextlib.contextmanager
def daemon(home, engine, enabled, *extra, appserver=True, broken_pending=False, peer_enabled=False):
    env = fixture_env(home,broken_pending,peer_enabled)
    env["DOXA_LORE"] = "1" if enabled else "0"
    if not appserver: env["DOXA_CODEX_APPSERVER"] = "0"
    process = subprocess.Popen([BINARY, "--runtime-dir", str(home / "runtime"),
        "--cwd", str(home), "--session-id", "native-lore-session", "--engine",engine,
        "--lore-python", sys.executable, "--linger", "20", *extra],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    wire = None
    try:
        registry = home / "runtime/registry/native-lore-session.json"
        until = time.monotonic() + 10
        while not registry.exists():
            assert process.poll() is None, process.stderr.read()
            assert time.monotonic() < until
            time.sleep(.01)
        entry = json.loads(registry.read_text())
        wire = socket.socket(socket.AF_UNIX)
        wire.settimeout(15)
        wire.connect(entry["daemon_socket"])
        with wire.makefile("r") as reader:
            def read():
                line = reader.readline()
                assert line, "native daemon disconnected"
                return json.loads(line)
            def send(value): wire.sendall((json.dumps(value) + "\n").encode())
            assert read()["lore_enabled"] is enabled
            send({"type":"attach", "cursor":None})
            yield send, read
            send({"type":"call","id":99,"method":"stop","params":{}})
            while True:
                frame = read()
                if frame.get("id") == 99:
                    assert frame["ok"] is True
                    break
            assert process.wait(timeout=10) == 0
    finally:
        if wire: wire.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        process.stderr.close()


def drive_turn(send, read, allow, title="LORE"):
    send({"type":"prompt","id":1,"text":"stage this local fixture proposal"})
    approved = False
    events = []
    while True:
        frame = read()
        event = frame.get("event", {})
        events.append(event)
        if event.get("type") == "needs_input":
            approved = True
            assert title in event["data"]["title"]
            assert event["data"]["require_full_review"] is True
            send({"type":"call","id":2,"method":"answer_needs_input","params":{
                "id":event["data"]["id"],"answer":{"decision":"allow" if allow else "deny"}}})
        if event.get("type") == "turn_done":
            assert event["data"]["is_error"] is False, events
            return approved, events


def assert_pending(home, engine, staged):
    proposals = list((home / "lore/pending").glob("*.json"))
    assert len(proposals) == int(staged)
    if staged:
        proposal = json.loads(proposals[0].read_text())
        assert proposal["session_id"] == "native-lore-session"
        assert proposal["source_engine"] == engine
        assert proposal["derived_by"] == "doxa-tool"
        assert proposal["text"] == "native isolated durable proposal"
    assert (home / "lore/USER.md").read_text() == "- isolated native user memory\n"
    assert all("native isolated durable proposal" not in row.read_text()
        for row in (home / "lore").glob("projects/**/MEMORY.md"))


@pytest.mark.parametrize("enabled,allow", [(True,True),(True,False),(False,True)])
def test_native_vendor_canonical_lore_catalog_and_pending_gate(native_home, enabled, allow):
    requests = []
    class Provider(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if enabled and len(requests) <= 2:
                names = {row["function"]["name"] for row in request["tools"]}
                assert {name for name in names if name.startswith("lore_")} == LORE_TOOLS
                args = {"text":"native isolated durable proposal","scope":"project"}
                if len(requests) == 1:
                    args["op_ctx"] = {"session_id":"forged","cwd":"/spoofed","source_engine":"forged"}
                else:
                    # The malformed request must leave no proposal before the valid call.
                    assert_pending(native_home,"deepseek",False)
                delta = {"tool_calls":[{"index":0,"id":f"tool-{len(requests)}","function":{
                    "name":"lore_remember","arguments":json.dumps(args)}}]}
                reason = "tool_calls"
            else:
                assert enabled or not any(row["function"]["name"].startswith("lore_") for row in request.get("tools",[]))
                delta = {"content":"local fixture answer"}
                reason = "stop"
            body = ("data: " + json.dumps({"choices":[{"finish_reason":reason,"delta":delta}]})
                    + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    provider = http.server.HTTPServer(("127.0.0.1",0), Provider)
    worker = threading.Thread(target=provider.serve_forever,daemon=True)
    worker.start()
    try:
        endpoint = "http://127.0.0.1:%d" % provider.server_port
        with daemon(native_home,"deepseek",enabled,"--vendor-endpoint",endpoint) as (send,read):
            approved,_ = drive_turn(send,read,allow)
            assert approved is enabled
            assert_pending(native_home,"deepseek", enabled and allow)
        assert len(requests) == 1 + 2 * int(enabled)
        if enabled:
            forged = json.loads(requests[1]["messages"][-1]["content"])
            assert forged.get("error") and not forged.get("staged")
            if allow:
                assert forged["error"] == "lore_remember: invalid arguments"
            result = json.loads(requests[2]["messages"][-1]["content"])
            assert bool(result.get("staged")) is allow
            if not allow:
                assert result.get("error")
    finally:
        provider.shutdown()
        worker.join(timeout=5)
        provider.server_close()


@pytest.mark.parametrize("enabled,allow", [(True,True),(True,False),(False,True)])
def test_native_codex_canonical_lore_catalog_and_pending_gate(native_home, enabled, allow):
    script = native_home / "codex-fixture"
    script.write_text(r'''#!/usr/bin/env python3
import json,os,pathlib,sys,tomllib
read=lambda:json.loads(sys.stdin.readline())
def send(value): print(json.dumps(value),flush=True)
init=read();send({'id':init['id'],'result':{'userAgent':'codex_cli_rs/0.156.1'}})
assert read()['method']=='initialized'
hooks=read();assert hooks['method']=='hooks/list'
overrides=[sys.argv[i+1] for i,x in enumerate(sys.argv[:-1]) if x=='-c']
config=next(tomllib.loads(x)['hooks'] for x in overrides if x.startswith('hooks='))
key=next(iter(config['state']))
row={'key':key,'command':config['PreCompact'][0]['hooks'][0]['command'],
 'handlerType':'command','enabled':True,'trustStatus':'trusted',
 'currentHash':config['state'][key]['trusted_hash'],'eventName':'preCompact',
 'source':'sessionFlags','timeoutSec':240,'async':False}
send({'id':hooks['id'],'result':{'data':[{'cwd':hooks['params']['cwds'][0],'hooks':[row]}],'errors':[]}})
thread=read();assert thread['method']=='thread/start'
for row in thread['params'].get('dynamicTools',[]):
 if row['name']=='mcp' or row['name'].startswith('mcp__'):
  send({'id':thread['id'],'error':{'code':-32600,'message':'dynamic tool name is reserved: '+row['name']}});sys.exit(1)
names={row['name'] for row in thread['params'].get('dynamicTools',[]) if row['name'].startswith('doxa_lore_')}
expected={'doxa_'+name for name in ('lore_belief_search','lore_belief_show','lore_belief_neighbours','lore_memory_list','lore_session_search','lore_remember')}
assert names==expected if __ENABLED__ else not names
send({'id':thread['id'],'result':{'thread':{'id':'thread-1'}}})
turn=read();send({'id':turn['id'],'result':{'turn':{'id':'turn_1'}}})
if __ENABLED__:
 send({'id':500,'method':'item/tool/call','params':{'threadId':'thread-1','turnId':'turn_1','callId':'call_1','tool':'doxa_lore_remember','arguments':{'text':'native isolated durable proposal','scope':'project','op_ctx':{'session_id':'forged','cwd':'/spoofed','source_engine':'forged'}}}})
 reply=read();assert reply['id']==500 and reply['result']['success'] is False
 text=reply['result']['contentItems'][0]['text']
 if __ALLOW__:
  marker,payload=text.split('\n',1)
  assert marker=='[DOXA LORE DATA -- UNTRUSTED]'
  result=json.loads(payload)
  assert result.get('error')=='lore_remember: invalid arguments' and not result.get('staged')
 else: assert text=='Tool was declined or unavailable'
 assert not list((pathlib.Path(os.environ['LORE_ROOT'])/'pending').glob('*.json'))
 send({'id':501,'method':'item/tool/call','params':{'threadId':'thread-1','turnId':'turn_1','callId':'call_2','tool':'doxa_lore_remember','arguments':{'text':'native isolated durable proposal','scope':'project'}}})
 reply=read();assert reply['id']==501 and reply['result']['success']==__ALLOW__
 text=reply['result']['contentItems'][0]['text']
 if __ALLOW__:
  marker,payload=text.split('\n',1)
  assert marker=='[DOXA LORE DATA -- UNTRUSTED]'
  assert json.loads(payload)['staged']
 else: assert text=='Tool was declined or unavailable'
send({'method':'item/agentMessage/delta','params':{'threadId':'thread-1','turnId':'turn_1','itemId':'answer','delta':'local fixture answer'}})
send({'method':'turn/completed','params':{'threadId':'thread-1','turn':{'id':'turn_1','status':'completed','error':None}}})
for line in sys.stdin: pass
'''.replace("__ENABLED__",repr(enabled)).replace("__ALLOW__",repr(allow)))
    script.chmod(0o700)
    with daemon(native_home,"codex",enabled,"--codex-bin",str(script)) as (send,read):
        approved,_ = drive_turn(send,read,allow)
        assert approved is enabled
        assert_pending(native_home,"codex", enabled and allow)


@pytest.mark.parametrize("enabled", [True,False])
def test_native_codex_exec_registers_canonical_mcp_with_frozen_memory_policy(native_home, enabled):
    script = native_home / "codex-exec-fixture"
    script.write_text(r'''#!/usr/bin/env python3
import json,os,pathlib,subprocess,sys,tomllib
sys.stdin.read()
overrides=[sys.argv[i+1] for i,x in enumerate(sys.argv[:-1]) if x=='-c']
config={}
for row in overrides:
 if not row.startswith('mcp_servers.doxa.'): continue
 parsed=tomllib.loads(row)['mcp_servers']['doxa']
 for key,value in parsed.items():
  if key=='env': config.setdefault('env',{}).update(value)
  else: config[key]=value
assert config['default_tools_approval_mode']=='approve'
assert config['env']['DOXA_MCP_SESSION_ID']=='native-lore-session'
assert config['env']['DOXA_MCP_PEER_SEND']=='0'
env={**os.environ,**config['env']}
server=subprocess.Popen([config['command'],*config['args']],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
def rpc(rid,method,params):
 server.stdin.write(json.dumps({'jsonrpc':'2.0','id':rid,'method':method,'params':params})+'\n');server.stdin.flush()
 reply=json.loads(server.stdout.readline());assert reply['id']==rid and 'error' not in reply
 return reply['result']
try:
 rpc(1,'initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'native-test','version':'1'}})
 tools=rpc(2,'tools/list',{})['tools']
 names={row['name'] for row in tools}
 expected={'lore_belief_search','lore_belief_show','lore_belief_neighbours','lore_memory_list','lore_session_search','lore_remember'}
 assert names==expected|{'peer_list','peer_history'} if __ENABLED__ else names=={'peer_list','peer_history'}
 if __ENABLED__:
  result=rpc(3,'tools/call',{'name':'lore_remember','arguments':{'text':'native isolated durable proposal','scope':'project','op_ctx':{'session_id':'forged','source_engine':'forged','cwd':'/spoofed'}}})
  assert result['isError'] is True
  forged=json.loads(result['content'][0]['text'])
  assert forged.get('error')=='lore_remember: invalid arguments' and not forged.get('staged')
  assert not list((pathlib.Path(os.environ['LORE_ROOT'])/'pending').glob('*.json'))
  result=rpc(4,'tools/call',{'name':'lore_remember','arguments':{'text':'native isolated durable proposal','scope':'project'}})
  assert result.get('isError',False) is False
  assert json.loads(result['content'][0]['text'])['staged']
finally:
 server.stdin.close();server.wait(timeout=5)
print(json.dumps({'type':'thread.started','thread_id':'thread-1'}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'local fixture answer'}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
'''.replace("__ENABLED__",repr(enabled)))
    script.chmod(0o700)
    with daemon(native_home,"codex",enabled,"--codex-bin",str(script),appserver=False) as (send,read):
        approved,_ = drive_turn(send,read,True)
        assert approved is False  # legacy exec MCP is noninteractive; canonical review still gates curated writes.
        assert_pending(native_home,"codex",enabled)


def test_native_vendor_caches_belief_count_and_reports_canonical_two_strikes(native_home):
    requests = []
    class Provider(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if len(requests)<=2:
                delta={"tool_calls":[{"index":0,"id":f"tool-{len(requests)}","function":{
                    "name":"lore_remember","arguments":json.dumps({"text":"cannot stage this proposal"})}}]}
                reason="tool_calls"
            else:
                delta={"content":"recovered after canonical containment"};reason="stop"
            body=("data: "+json.dumps({"choices":[{"finish_reason":reason,"delta":delta}]})+"\n\ndata: [DONE]\n\n").encode()
            self.send_response(200);self.send_header("Content-Type","text/event-stream")
            self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
    server=http.server.HTTPServer(("127.0.0.1",0),Provider)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        endpoint=f"http://127.0.0.1:{server.server_port}"
        with daemon(native_home,"deepseek",True,"--vendor-endpoint",endpoint,broken_pending=True) as (send,read):
            send({"type":"call","id":7,"method":"status","params":{}})
            while True:
                frame=read()
                if frame.get("id")==7:
                    assert frame["status"]["belief_count"]==0
                    assert frame["status"]["disabled_tools"]==[]
                    break
            approved,events=drive_turn(send,read,True)
            assert approved
            disabled=[row for row in events if row.get("type")=="tool_disabled"]
            assert len(disabled)==1 and disabled[0]["data"]["name"]=="lore_remember"
            send({"type":"call","id":8,"method":"status","params":{}})
            while True:
                frame=read()
                if frame.get("id")==8:
                    assert frame["status"]["disabled_tools"]==["lore_remember"]
                    break
            assert (native_home/"lore/pending").read_text()=="isolated broken backend"
        assert len(requests)==3
    finally:
        server.shutdown();worker.join(timeout=5);server.server_close()


def test_native_vendor_peer_refusals_return_errors_and_allow_a_recovery_tool(native_home):
    requests=[]
    class Provider(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if len(requests)<=3:
                name="mcp__doxa__peer_send" if len(requests)<3 else "mcp__doxa__peer_list"
                arguments={"target":"no-such-isolated-peer","text":"bounded fixture message"} if len(requests)<3 else {}
                delta={"tool_calls":[{"index":0,"id":f"peer-{len(requests)}","function":{
                    "name":name,"arguments":json.dumps(arguments)}}]};reason="tool_calls"
            else:
                delta={"content":"recovered after retryable peer refusal"};reason="stop"
            body=("data: "+json.dumps({"choices":[{"finish_reason":reason,"delta":delta}]})+"\n\ndata: [DONE]\n\n").encode()
            self.send_response(200);self.send_header("Content-Type","text/event-stream")
            self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
    server=http.server.HTTPServer(("127.0.0.1",0),Provider)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        with daemon(native_home,"deepseek",False,"--vendor-endpoint",f"http://127.0.0.1:{server.server_port}",peer_enabled=True) as (send,read):
            approved,events=drive_turn(send,read,True,title="peer")
            assert approved and len(requests)==4
            errors=[row for row in events if row.get("type")=="tool_result" and row["data"].get("is_error")]
            assert len(errors)==2
            assert all("no live same-scope peer matches target" in row["data"]["result_summary"] for row in errors)
            assert not any(row.get("type")=="tool_disabled" for row in events)
            tools=[row for row in requests[-1]["messages"] if row.get("role")=="tool"]
            assert len(tools)==3 and all(json.loads(row["content"])["error"].startswith("peer_send:") for row in tools[:2])
    finally:
        server.shutdown();worker.join(timeout=5);server.server_close()

#!/usr/bin/env python3
"""Local native protocol peer; no authentication files, shell calls or network."""
import json, os, signal, socket, sys, threading, tomllib
from pathlib import Path

codex = 'app-server' in sys.argv
provider = 'codex' if codex else 'claude'
expected = provider + '-fixture-1'
mode_file = Path(os.environ['FIXTURE_MODE_FILE'])

def mode():
    return mode_file.read_text().strip()

def capture(kind, value):
    with open(os.environ['FIXTURE_LOG'], 'a') as log:
        log.write(json.dumps({'provider': provider, 'pid': os.getpid(), 'kind': kind, 'value': value})+'\n')

def send(value):
    print(json.dumps(value), flush=True)

def reply(row, value):
    send({'id':row['id'], 'result':value})

def fixture_config():
    overrides = [sys.argv[i+1] for i, item in enumerate(sys.argv[:-1]) if item == '-c']
    for flag in ('features.token_budget=false', 'features.async_compaction=false',
                 'agents.enabled=false', 'features.multi_agent_v2=false'):
        assert flag in overrides, flag
    def merge(target, incoming):
        for key, value in incoming.items():
            if isinstance(value, dict):
                merge(target.setdefault(key, {}), value)
            else:
                target[key] = value
    config = {}
    for value in overrides:
        merge(config, tomllib.loads(value))
    return config

fd = os.environ.pop('DOXA_CODEX_OWNER_FD', None)
if fd:
    owner = socket.socket(fileno=int(fd)); owner.settimeout(5)
    owner.sendall(b'DOXA_PROVIDER_OWNER_V1\n'); assert owner.recv(1) == b'G'
    owner.settimeout(None)
    group = os.getpgrp()
    def watch():
        try:
            while owner.recv(1): pass
        finally:
            os.killpg(group, signal.SIGKILL)
    threading.Thread(target=watch,daemon=True).start()

session = sys.argv[sys.argv.index('--session-id')+1] if not codex else 'native-thread-fixture'
model = sys.argv[sys.argv.index('--model')+1] if '--model' in sys.argv else expected
effort = sys.argv[sys.argv.index('--effort')+1] if '--effort' in sys.argv else 'high'
active = False

def finish():
    if mode() == 'hold': return
    if mode() == 'source-change':
        path = Path(os.environ['FIXTURE_CANONICAL_MESSAGES'])
        value = json.loads(path.read_text())
        value['messages'].append({'role':'user','content':'external canonical edit'})
        path.write_text(json.dumps(value))
    actual = 'unreviewed-provider-model' if mode() == 'wrong-model' else expected
    text = provider.capitalize()+' answer'
    inputs = 2000000 if mode() == 'excess-usage' else 7
    if codex:
        send({'method':'item/completed','params':{'threadId':session,'turnId':'turn-1','item':{'type':'agentMessage','id':'answer-1','text':text}}})
        if mode() != 'missing-usage':
            send({'method':'thread/tokenUsage/updated','params':{'threadId':session,'turnId':'turn-1','tokenUsage':{
                'total':{'inputTokens':inputs,'outputTokens':5,'cachedInputTokens':0,'totalTokens':inputs+5},
                'last':{'inputTokens':inputs,'outputTokens':5,'cachedInputTokens':0,'totalTokens':inputs+5},'modelContextWindow':131072}}})
        send({'method':'turn/completed','params':{'threadId':session,'turn':{'id':'turn-1','status':'completed','error':None}}})
    else:
        send({'type':'stream_event','session_id':session,'event':{'type':'content_block_delta','delta':{'type':'text_delta','text':text}}})
        send({'type':'assistant','session_id':session,'message':{'role':'assistant','model':actual,'content':[{'type':'text','text':text}]}})
        usage = {} if mode() == 'missing-usage' else {'input_tokens':inputs,'output_tokens':5,'cache_read_input_tokens':0,'cache_creation_input_tokens':0}
        send({'type':'result','session_id':session,'is_error':False,'num_turns':1,'usage':usage})

def peer():
    if codex:
        send({'id':'peer-1','method':'item/tool/call','params':{'threadId':session,'turnId':'turn-1','callId':'peer-call-1','tool':'doxa_peer_list','arguments':{}}})
    else:
        send({'type':'control_request','request_id':'peer-1','request':{'subtype':'mcp_message','server_name':'doxa',
            'message':{'jsonrpc':'2.0','id':'peer-call-1','method':'tools/call','params':{'name':'peer_list','arguments':{}}}}})

for line in sys.stdin:
    row=json.loads(line)
    if codex:
        method=row.get('method')
        capture(method or 'response',row.get('params',row.get('result',{})))
        if method=='initialize': reply(row,{'userAgent':'doxa_codex_rs/__DOXA_SUPPORTED_CODEX_VERSION__ (doxa-precompact-fail-closed-v1; upstream fixture)'})
        elif method=='initialized': pass
        elif method=='account/read': reply(row,{'account':None if mode()=='no-auth' else {'type':'chatgpt'},'requiresOpenaiAuth':True})
        elif method=='model/list': reply(row,{'data':[{'model':expected,'isDefault':True,'supportedReasoningEfforts':[] if mode()=='missing-effort' else [{'reasoningEffort':'high'}],'defaultReasoningEffort':'high'}],'nextCursor':None})
        elif method=='config/read': reply(row,{'config':fixture_config(),'origins':{},'layers':None})
        elif method=='hooks/list':
            overrides=[sys.argv[i+1] for i,v in enumerate(sys.argv[:-1]) if v=='-c']
            hooks=next(tomllib.loads(v)['hooks'] for v in overrides if v.startswith('hooks='))
            key=next(iter(hooks['state']))
            h={'key':key,'command':hooks['PreCompact'][0]['hooks'][0]['command'],'handlerType':'command','enabled':True,'trustStatus':'trusted',
               'currentHash':hooks['state'][key]['trusted_hash'],'eventName':'preCompact','source':'sessionFlags','timeoutSec':240,'async':False}
            reply(row,{'data':[{'cwd':row['params']['cwds'][0],'hooks':[h]}],'errors':[]})
        elif method=='thread/start':
            assert any(t['name']=='doxa_peer_list' for t in row['params']['dynamicTools'])
            assert row['params']['approvalPolicy']=='on-request'
            assert row['params']['sandbox']=='workspace-write'
            reply(row,{'thread':{'id':session},'model':'unreviewed-provider-model' if mode()=='wrong-model' else expected})
        elif method=='turn/start':
            active=True;reply(row,{'turn':{'id':'turn-1'}})
            send({'id':'approval-1','method':'item/commandExecution/requestApproval','params':{'threadId':session,'turnId':'turn-1','itemId':'cmd-1','command':'echo local fixture','cwd':os.getcwd()}})
        elif not method and row.get('id')=='approval-1':
            assert row['result']['decision']=='accept';peer()
        elif not method and row.get('id')=='peer-1':
            capture('peer-result',row['result']);finish()
        elif method=='turn/interrupt':
            reply(row,{});send({'method':'turn/completed','params':{'threadId':session,'turn':{'id':'turn-1','status':'interrupted','error':{'message':'local interruption'}}}})
        else: send({'id':row.get('id'),'error':{'code':-32601,'message':'unknown local fixture RPC'}})
    elif row['type']=='control_request':
        req=row['request'];sub=req['subtype'];capture(sub,req);response={}
        if sub=='initialize': response={'models':[{'value':expected,'resolvedModel':'different-resolved-model' if mode()=='alias' else expected,'supportedEfforts':[] if mode()=='missing-effort' else ['high']}],
            'account':{'apiProvider':'firstParty'} if mode()=='no-auth' else {'apiProvider':'firstParty','apiKeySource':'local-fixture'}}
        elif sub=='get_settings': response={'applied':{'model':model,'effort':effort}}
        elif sub=='get_context_usage': response={'totalTokens':12,'maxTokens':131072,'percentage':.01}
        elif sub=='set_permission_mode': response={'mode':req['mode']}
        send({'type':'control_response','response':{'subtype':'success','request_id':row['request_id'],'response':response}})
        if sub=='interrupt' and active: send({'type':'result','session_id':session,'is_error':True})
    elif row['type']=='control_response':
        capture('response',row['response']);rid=row['response']['request_id']
        if rid=='approval-1': assert row['response']['response']['behavior']=='allow';peer()
        elif rid=='peer-1':
            capture('peer-result',row['response'])
            send({'type':'user','session_id':session,'message':{'role':'user','content':[
                {'type':'tool_result','tool_use_id':'peer-call-1','content':'local peer tool completed'}]}})
            finish()
    elif row['type']=='user':
        active=True;capture('prompt',row['message']['content'])
        send({'type':'control_request','request_id':'approval-1','request':{'subtype':'can_use_tool','tool_name':'Bash','input':{'command':'echo local fixture'}}})

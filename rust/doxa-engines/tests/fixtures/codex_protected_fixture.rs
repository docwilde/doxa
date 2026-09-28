fn fixture(scenario: &str) -> (tempfile::TempDir, AppServerOptions, CompactGate) {
    let cache = std::env::var_os("TMPDIR").map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::PathBuf::from(std::env::var_os("HOME").unwrap()).join(".cache/doxa-tests"));
    fs::create_dir_all(&cache).unwrap();
    let dir = tempfile::tempdir_in(cache).unwrap();
    fs::write(dir.path().join("scenario"), scenario).unwrap();
    let executable = dir.path().join("fake-codex");
    fs::write(&executable, r#"#!/usr/bin/env python3
import json, sys, tomllib
from pathlib import Path
mode=Path('scenario').read_text()
def read():
    line=sys.stdin.readline()
    if not line: sys.exit(0)
    with Path('requests.jsonl').open('a') as log: log.write(line)
    return json.loads(line)
def send(x): print(json.dumps(x),flush=True)
def notice(method,**params): send({'method':method,'params':dict(threadId='thread-actual',**params)})
init=read(); assert init['method']=='initialize'
agent='Codex Desktop/0.156.1 (Ubuntu 26.4.0; x86_64) dumb (doxa; 2.0.0-alpha.37)' if mode=='compact' else 'codex_cli_rs/'+('0.1.0' if mode=='version' else '0.156.1')
send({'id':init['id'],'result':{'userAgent':agent}})
assert read()['method']=='initialized'
if mode=='version':
    # Any subsequent request is evidence of an unsafe initialization order.
    if sys.stdin.readline(): Path('unsafe-after-version').write_text('request')
    sys.exit(0)
args=sys.argv[1:]; overrides=[args[i+1] for i,x in enumerate(args[:-1]) if x=='-c']
hooks=next(tomllib.loads(x)['hooks'] for x in overrides if x.startswith('hooks='))
key=next(iter(hooks['state'])); command=hooks['PreCompact'][0]['hooks'][0]['command']
query=read(); assert query['method']=='config/read'
assert 'features.token_budget=false' in overrides
send({'id':query['id'],'result':{'config':{'features':{'token_budget':mode=='token-budget'}},'origins':{},'layers':None}})
if mode=='token-budget':
    if sys.stdin.readline(): Path('unsafe-after-token-budget').write_text('request')
    sys.exit(0)
query=read(); assert query['method']=='hooks/list' 
row={'key':key,'command':command,'handlerType':'command','enabled':True,'trustStatus':'trusted','currentHash':hooks['state'][key]['trusted_hash'],'eventName':'preCompact','source':'sessionFlags','timeoutSec':240,'async':False}
if mode=='hash': row['currentHash']='sha256:wrong'
send({'id':query['id'],'result':{'data':[{'cwd':str(Path.cwd()),'hooks':[row]}],'errors':[]}})
if mode=='hash':
    if sys.stdin.readline(): Path('unsafe-after-hash').write_text('request')
    sys.exit(0)
thread=read(); assert thread['method']=='thread/start'
assert thread['params']['approvalPolicy']=='on-request'
for tool in thread['params'].get('dynamicTools',[]):
    name=tool['name']
    if name=='mcp' or name.startswith('mcp__'):
        send({'id':thread['id'],'error':{'code':-32600,'message':'dynamic tool name is reserved: '+name}})
        sys.exit(1)
    assert 0<len(name)<=128 and all(char.isascii() and (char.isalnum() or char in '_-') for char in name)
assert len({tool['name'] for tool in thread['params'].get('dynamicTools',[])})==len(thread['params'].get('dynamicTools',[]))
if mode.startswith('peer'): assert {x['name'] for x in thread['params']['dynamicTools']}=={'doxa_peer_list','doxa_peer_send','doxa_peer_history'}
send({'id':thread['id'],'result':{'thread':{'id':'thread-actual'},'model':'gpt-5.5'}})
if mode=='model':
    if sys.stdin.readline(): Path('unsafe-model-turn').write_text('request')
    sys.exit(0)
operation=read()
if operation['method']=='thread/read':
    assert operation['params']=={'threadId':'thread-actual','includeTurns':False}
    source=Path.cwd()/'codex-home'/'sessions'/'thread.jsonl'
    source.parent.mkdir(parents=True,exist_ok=True)
    source.write_text(json.dumps({'type':'session_meta','payload':{'id':'thread-actual'}})+'\n')
    Path('source-before').write_bytes(source.read_bytes())
    if mode=='missing-source': source.unlink()
    if mode=='foreign-source': source.write_text(json.dumps({'type':'session_meta','payload':{'id':'foreign'}})+'\n')
    send({'id':operation['id'],'result':{'thread':{'id':'foreign' if mode=='foreign-thread' else 'thread-actual','path':str(source)}}})
    operation=read()
if mode=='unreviewed-auto':
    assert operation['method']=='turn/start'
    send({'id':operation['id'],'result':{'turn':{'id':'turn-auto'}}})
    notice('item/completed',turnId='turn-auto',item={'id':'compact','type':'contextCompaction'})
    import time
    time.sleep(0.3); Path('survived-unreviewed-auto').write_text('unsafe')
elif mode.startswith('alias'):
    assert operation['method']=='turn/start'
    send({'id':operation['id'],'result':{'turn':{'id':'turn-alias'}}})
    names=[tool['name'] for tool in thread['params']['dynamicTools']]
    assert len(names)==9
    assert set(names)=={'doxa_'+name for name in ('peer_list','peer_send','peer_history','lore_belief_search','lore_belief_show','lore_belief_neighbours','lore_memory_list','lore_session_search','lore_remember')}
    if mode=='alias-forged': names=['mcp__doxa__peer_list']
    for index,name in enumerate(names):
        send({'id':100+index,'method':'item/tool/call','params':{'threadId':'thread-actual','turnId':'turn-alias','callId':'alias-'+str(index),'namespace':'foreign' if mode=='alias-namespace' else None,'tool':name,'arguments':{}}})
        reply=read()
        if mode!='alias-all':
            assert reply['error']['code']==-32602
            break
        assert reply['id']==100+index and reply['result']['success']
    Path('alias-replies').write_text('verified')
    notice('turn/completed',turn={'id':'turn-alias','status':'completed','error':None})
elif mode.startswith('peer'):
    assert operation['method']=='turn/start'
    send({'id':operation['id'],'result':{'turn':{'id':'turn-peer'}}})
    send({'id':71,'method':'item/tool/call','params':{'threadId':'thread-actual','turnId':'turn-peer','callId':'peer-call','namespace':None,'tool':'doxa_peer_send','arguments':{'target':'peer-exact','text':'hello'}}})
    response=read()
    if mode=='peer-cancel':
        sys.exit(0)
    assert response['id']==71
    assert response['result']['success']==(mode=='peer-allow')
    Path('peer-reply').write_text(json.dumps(response))
    notice('turn/completed',turn={'id':'turn-peer','status':'completed','error':None})
else:
    assert operation['method']=='thread/compact/start' and operation['params']=={'threadId':'thread-actual'}
    send({'id':operation['id'],'result':{}})
    notice('turn/started',turn={'id':'turn-compact','status':'inProgress'})
    run={'eventName':'preCompact','source':'sessionFlags','sourcePath':'/<session-flags>/config.toml','handlerType':'command','executionMode':'sync','status':'failed' if mode=='failed' else 'stopped' if mode.startswith('blocked') else 'completed'}
    if mode=='order': notice('item/completed',turnId='turn-compact',item={'id':'compact','type':'contextCompaction'})
    if mode=='foreign': send({'method':'hook/completed','params':{'threadId':'thread-other','turnId':'turn-compact','run':run}})
    elif mode=='stale-turn': notice('hook/completed',turnId='turn-other',run=run)
    else: notice('hook/completed',turnId='turn-compact',run=run)
    if not mode.startswith('blocked'):
        notice('item/completed',turnId='turn-compact',item={'id':'compact','type':'contextCompaction'})
        if mode=='compact': notice('thread/tokenUsage/updated',turnId='turn-compact',tokenUsage={'last':{'inputTokens':3,'outputTokens':4},'total':{'inputTokens':10,'outputTokens':20,'cachedInputTokens':2}})
    if mode=='blocked-usage': notice('thread/tokenUsage/updated',turnId='turn-compact',tokenUsage={'last':{'inputTokens':3,'outputTokens':4},'total':{'inputTokens':10,'outputTokens':20}})
    notice('turn/completed',turn={'id':'turn-compact','status':'completed','error':None})
    if mode=='blocked':
        followup=read(); assert followup['method']=='turn/start'
        send({'id':followup['id'],'result':{'turn':{'id':'turn-followup'}}})
        Path('usable-after-blocked').write_text('verified')
        notice('turn/completed',turn={'id':'turn-followup','status':'completed','error':None})
    if mode=='failed':
        # Driver must stop this process, rather than merely hiding success.
        import time
        time.sleep(0.3); Path('survived-failed-hook').write_text('unsafe')
"#).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    let gate_dir = dir.path().join("gate"); fs::create_dir(&gate_dir).unwrap(); fs::set_permissions(&gate_dir, fs::Permissions::from_mode(0o700)).unwrap();
    let gate = CompactGate::prepare(&gate_dir, &std::env::current_exe().unwrap(), &dir.path().join("codex-home"), dir.path(), "doxa-fixture", "0.156.1").unwrap();
    let options = AppServerOptions { executable, cwd:dir.path().to_owned(), model:None, sandbox:SandboxMode::WorkspaceWrite, resume_thread:None, turn_timeout:Duration::from_secs(3) };
    (dir, options, gate)
}

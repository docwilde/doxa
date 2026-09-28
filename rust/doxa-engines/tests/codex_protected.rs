#![cfg(unix)]
use doxa_engines::{codex_appserver::{AppServerDriver, AppServerOptions}, codex_compact::CompactGate, codex_driver::SandboxMode, codex_interaction::InputInbox};
use serde_json::{json, Value};
use std::{fs, os::unix::fs::PermissionsExt, sync::{Arc, atomic::{AtomicUsize, Ordering}}, time::Duration};
use tokio_util::sync::CancellationToken;

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
def read(): return json.loads(sys.stdin.readline())
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
if mode.startswith('alias'):
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

#[tokio::test]
async fn authoritative_version_and_hook_hash_refuse_before_thread_creation() {
    for mode in ["version", "hash"] {
        let (dir, options, gate) = fixture(mode);
        assert!(AppServerDriver::spawn_protected(options, str::to_owned, false, gate).await.is_err());
        assert!(!dir.path().join(format!("unsafe-after-{mode}")).exists());
    }
}

#[tokio::test]
async fn protected_initial_model_is_verified_before_any_turn() {
    for requested in ["gpt-5.5", "gpt-6-astra"] {
        let (dir, mut options, gate)=fixture("model");
        options.model=Some(requested.into());
        let result=AppServerDriver::spawn_protected(options,str::to_owned,false,gate).await;
        assert_eq!(result.is_ok(),requested=="gpt-5.5");
        if let Ok(mut driver)=result {driver.shutdown().await;}
        assert!(!dir.path().join("unsafe-model-turn").exists());
    }
}

#[tokio::test]
async fn manual_compaction_binds_actual_thread_and_requires_review_before_completion() {
    for mode in ["compact", "order", "failed", "foreign", "stale-turn", "blocked"] {
        let (dir, options, gate) = fixture(mode);
        let mut driver = AppServerDriver::spawn_protected(options, str::to_owned, false, gate).await.unwrap();
        let manifest:Value=serde_json::from_slice(&fs::read(dir.path().join("gate/compact-session.json")).unwrap()).unwrap();
        assert_eq!(manifest["provider_thread"], "thread-actual");
        let mut events=Vec::new();
        let result=driver.compact(&CancellationToken::new(), |e| events.push(e)).await;
        if mode == "blocked" {
            assert!(matches!(result,Err(doxa_engines::codex_appserver::AppServerError::CompactionBlocked)));
            assert_eq!(driver.thread_id(),"thread-actual");
            assert!(driver.run_turn("fixture followup",&CancellationToken::new(),|_|{}).await.is_ok());
            assert!(dir.path().join("usable-after-blocked").exists());
        }
        assert_eq!(result.is_ok(), mode=="compact");
        assert_eq!(events.iter().any(|e| e.kind=="compaction_done"),mode=="compact");
        if mode=="compact"{let done=events.iter().find(|event|event.kind=="turn_done").unwrap();assert_eq!(done.data["usage_complete"],true);assert_eq!(done.data["input_tokens"],10);assert_eq!(done.data["output_tokens"],20);}
        if mode=="failed" {
            tokio::time::sleep(Duration::from_millis(400)).await;
            assert!(!dir.path().join("survived-failed-hook").exists());
            assert!(driver.compact(&CancellationToken::new(), |_|{}).await.is_err());
        }
        driver.shutdown().await;
    }
}

#[tokio::test]
async fn peer_dynamic_tool_requires_matching_single_use_permission_and_cancels_pending() {
    for mode in ["peer-allow", "peer-deny", "peer-cancel"] {
        let (dir, options, gate)=fixture(mode);
        let mut driver=AppServerDriver::spawn_protected(options,str::to_owned,true,gate).await.unwrap();
        let inbox=Arc::new(InputInbox::default());
        let count=Arc::new(AtomicUsize::new(0)); let called=count.clone();
        let handler:doxa_engines::peer_tools::Handler=Arc::new(move |rpc:&str,args:&Value| {
            assert_eq!(rpc,"msg"); assert_eq!(args,&json!({"target":"peer-exact","text":"hello"}));
            called.fetch_add(1,Ordering::SeqCst); Ok(json!({"delivered":true}))
        });
        let cancel=CancellationToken::new(); let trigger=cancel.clone(); let input=inbox.clone();
        let result=driver.run_turn_interactive("hello",&cancel,|event| {
            if event.kind=="needs_input" {
                if mode=="peer-cancel" { trigger.cancel(); }
                else { input.answer(event.data["id"].as_str().unwrap(),&json!({"decision":if mode=="peer-allow" {"allow"} else {"deny"}})).unwrap(); }
            }
        },|frame| inbox.begin_peer(frame,str::to_owned,handler.clone()).map(Some)).await;
        assert_eq!(result.is_ok(),mode!="peer-cancel");
        assert_eq!(count.load(Ordering::SeqCst),usize::from(mode=="peer-allow"));
        if mode!="peer-cancel" { assert!(dir.path().join("peer-reply").exists()); }
        inbox.clear(); driver.shutdown().await;
    }
}

#[tokio::test]
async fn codex_dynamic_aliases_route_all_canonical_handlers_and_refuse_unadvertised_namespaces() {
    let names = ["peer_list", "peer_send", "peer_history", "lore_belief_search", "lore_belief_show",
        "lore_belief_neighbours", "lore_memory_list", "lore_session_search", "lore_remember"];
    for mode in ["alias-all", "alias-forged", "alias-namespace"] {
        let (dir, options, gate) = fixture(mode);
        let definitions = names[3..].iter().map(|name| json!({"type":"function",
            "name":format!("mcp__doxa__{name}"), "description":"fixture operator",
            "inputSchema":{"type":"object","properties":{},"additionalProperties":false}})).collect();
        let mut driver = AppServerDriver::spawn_protected_with_agent_tools(options, str::to_owned, true, gate, definitions).await.unwrap();
        let mut called = Vec::new();
        let mut events = Vec::new();
        let result = driver.run_turn_interactive("fixture", &CancellationToken::new(), |event| events.push(event), |frame| {
            let canonical = frame["params"]["tool"].as_str().unwrap();
            assert_eq!(canonical, format!("mcp__doxa__{}", names[called.len()]));
            called.push(canonical.to_owned());
            let (sender, receiver) = tokio::sync::oneshot::channel();
            sender.send(json!({"success":true,"contentItems":[]})).unwrap();
            Ok(Some((doxa_engines::EngineEvent::new("needs_input", json!({"id":"fixture-gate"})), receiver)))
        }).await;
        assert_eq!(result.is_ok(), mode == "alias-all");
        assert_eq!(called.len(), if mode == "alias-all" { 9 } else { 0 });
        if mode == "alias-all" {
            let displayed: Vec<_> = events.iter().filter(|event| event.kind == "tool_call")
                .map(|event| event.data["name"].as_str().unwrap()).collect();
            assert_eq!(displayed, called.iter().map(String::as_str).collect::<Vec<_>>());
        }
        tokio::time::timeout(Duration::from_secs(1), async {
            while !dir.path().join("alias-replies").exists() { tokio::time::sleep(Duration::from_millis(5)).await; }
        }).await.expect("fixture received the matching dynamic-tool reply");
        driver.shutdown().await;
    }
    // Shared Claude MCP/vendor handler contracts retain their canonical names.
    assert_eq!(doxa_engines::peer_tools::definitions()[0]["name"], "mcp__doxa__peer_list");
}

#[tokio::test]
async fn manual_compaction_never_invents_missing_or_inconsistent_accounting(){
    for mode in ["compact-no-usage","blocked-usage"]{
        let (_dir,options,gate)=fixture(mode);
        let mut driver=AppServerDriver::spawn_protected(options,str::to_owned,false,gate).await.unwrap();
        let mut events=vec![];let result=driver.compact(&CancellationToken::new(),|event|events.push(event)).await;
        if mode=="compact-no-usage"{assert!(result.is_ok());let done=events.iter().find(|event|event.kind=="turn_done").unwrap();assert_eq!(done.data["usage_complete"],false);assert!(done.data["input_tokens"].is_null());}
        else{assert!(result.is_err());assert!(!matches!(result,Err(doxa_engines::codex_appserver::AppServerError::CompactionBlocked)));assert!(!events.iter().any(|event|event.kind=="compaction_done"));}
        driver.shutdown().await;
    }
}

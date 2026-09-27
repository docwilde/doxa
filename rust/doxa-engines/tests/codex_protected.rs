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
send({'id':init['id'],'result':{'userAgent':'codex_cli_rs/'+('0.1.0' if mode=='version' else '0.156.1')}})
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
if mode.startswith('peer'): assert {x['name'] for x in thread['params']['dynamicTools']}=={'mcp__doxa__peer_list','mcp__doxa__peer_send','mcp__doxa__peer_history'}
send({'id':thread['id'],'result':{'thread':{'id':'thread-actual'},'model':'gpt-5.5'}})
if mode=='model':
    if sys.stdin.readline(): Path('unsafe-model-turn').write_text('request')
    sys.exit(0)
operation=read()
if mode.startswith('peer'):
    assert operation['method']=='turn/start'
    send({'id':operation['id'],'result':{'turn':{'id':'turn-peer'}}})
    send({'id':71,'method':'item/tool/call','params':{'threadId':'thread-actual','turnId':'turn-peer','callId':'peer-call','namespace':None,'tool':'mcp__doxa__peer_send','arguments':{'target':'peer-exact','text':'hello'}}})
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
    run={'eventName':'preCompact','source':'sessionFlags','sourcePath':'/<session-flags>/config.toml','handlerType':'command','executionMode':'sync','status':'failed' if mode=='failed' else 'completed'}
    if mode=='order': notice('item/completed',turnId='turn-compact',item={'id':'compact','type':'contextCompaction'})
    if mode=='foreign': send({'method':'hook/completed','params':{'threadId':'thread-other','turnId':'turn-compact','run':run}})
    elif mode=='stale-turn': notice('hook/completed',turnId='turn-other',run=run)
    else: notice('hook/completed',turnId='turn-compact',run=run)
    notice('item/completed',turnId='turn-compact',item={'id':'compact','type':'contextCompaction'})
    notice('turn/completed',turn={'id':'turn-compact','status':'completed','error':None})
    if mode=='failed':
        # Driver must stop this process, rather than merely hiding success.
        import time
        time.sleep(0.3); Path('survived-failed-hook').write_text('unsafe')
"#).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    let gate_dir = dir.path().join("gate"); fs::create_dir(&gate_dir).unwrap(); fs::set_permissions(&gate_dir, fs::Permissions::from_mode(0o700)).unwrap();
    let gate = CompactGate::prepare(&gate_dir, std::path::Path::new("/usr/bin/python3"), &dir.path().join("codex-home"), dir.path(), "doxa-fixture", "0.156.1").unwrap();
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
    for mode in ["compact", "order", "failed", "foreign", "stale-turn"] {
        let (dir, options, gate) = fixture(mode);
        let mut driver = AppServerDriver::spawn_protected(options, str::to_owned, false, gate).await.unwrap();
        let manifest:Value=serde_json::from_slice(&fs::read(dir.path().join("gate/compact-session.json")).unwrap()).unwrap();
        assert_eq!(manifest["provider_thread"], "thread-actual");
        let mut events=Vec::new();
        let result=driver.compact(&CancellationToken::new(), |e| events.push(e)).await;
        assert_eq!(result.is_ok(), mode=="compact");
        assert_eq!(events.iter().any(|e| e.kind=="compaction_done"),mode=="compact");
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

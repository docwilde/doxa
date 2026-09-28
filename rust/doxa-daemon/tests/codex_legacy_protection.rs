#![cfg(unix)]
use doxa_transcript::TranscriptStore;
use serde_json::{json, Value};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    os::unix::{fs::PermissionsExt, net::UnixStream},
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant},
};
fn receive(reader: &mut BufReader<UnixStream>) -> Value {
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    assert!(!line.is_empty());
    serde_json::from_str(&line).unwrap()
}
fn send(socket: &mut UnixStream, frame: Value) {
    writeln!(socket, "{frame}").unwrap();
}
fn seed(root: &Path) -> TranscriptStore {
    let slug: String = root
        .to_string_lossy()
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect();
    let store = TranscriptStore::new(&root.join("projects"), &slug, "legacy-session").unwrap();
    store
        .append(
            json!({"type":"user","message":{"role":"user","content":"owned prior user"}}),
            "codex",
            str::to_owned,
        )
        .unwrap();
    store.append(json!({"type":"assistant","message":{"role":"assistant","content":"owned prior answer"}}),"codex",str::to_owned).unwrap();
    store.write_thread(json!({"thread_id":"legacy-thread","session_id":"legacy-session","cwd":root,
        "turn_incomplete":false,"transport":"exec","lore_enabled":true,"peer_tools":false,"lore_tools":false,
        "model":null,"effort":null,"opaque_legacy_field":"retained"}).as_object().unwrap().clone(),str::to_owned).unwrap();
    store
}
fn command(root: &Path, provider: &Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"));
    command
        .args([
            "--runtime-dir",
            root.to_str().unwrap(),
            "--cwd",
            root.to_str().unwrap(),
            "--session-id",
            "legacy-session",
            "--engine",
            "codex",
            "--codex-bin",
            provider.to_str().unwrap(),
            "--resume",
            "true",
            "--linger",
            "10",
        ])
        .env("HOME", root.join("home"))
        .env("DOXA_HOME", root.join("doxa-home"))
        .env("CODEX_HOME", root.join("codex-home"))
        .env("LORE_ROOT", root.join("lore"))
        .env("LORE_PROJECTS_DIR", root.join("projects"))
        .env("LORE_CODEX_SESSIONS_DIR", root.join("codex-sessions"))
        .env("LORE_SKILLS_DIR", root.join("skills"))
        .env("LORE_DISABLE_SYNC", "1")
        .env("LORE_DISABLE_REVIEW", "1")
        .env("DOXA_CODEX_APPSERVER", "0")
        .env_remove("DOXA_CODEX_MIGRATE_APPSERVER")
        .env_remove("DOXA_LORE")
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    command
}
fn connect(root: &Path) -> (BufReader<UnixStream>, UnixStream) {
    let registry = root.join("registry/legacy-session.json");
    let deadline = Instant::now() + Duration::from_secs(10);
    while !registry.exists() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    let entry: Value = serde_json::from_slice(&fs::read(registry).unwrap()).unwrap();
    let socket = UnixStream::connect(entry["daemon_socket"].as_str().unwrap()).unwrap();
    socket
        .set_read_timeout(Some(Duration::from_secs(30)))
        .unwrap();
    let mut reader = BufReader::new(socket.try_clone().unwrap());
    receive(&mut reader);
    (reader, socket)
}
#[test]
fn legacy_exec_refusal_preserves_saved_thread_transcript_and_never_spawns_provider() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let store = seed(root);
    let thread_before = fs::read(store.thread_path()).unwrap();
    let transcript_before = fs::read(store.transcript_path()).unwrap();
    let provider = root.join("provider");
    fs::write(
        &provider,
        "#!/bin/sh\nprintf unsafe > provider-ran\nexit 1\n",
    )
    .unwrap();
    fs::set_permissions(&provider, fs::Permissions::from_mode(0o700)).unwrap();
    let mut child = command(root, &provider).spawn().unwrap();
    let (mut reader, mut socket) = connect(root);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    for prompt in ["legacy turn must remain read-only", "/compact"] {
        send(&mut socket, json!({"type":"prompt","id":1,"text":prompt}));
        loop {
            let frame = receive(&mut reader);
            if frame["event"]["type"] == "turn_done" {
                assert_eq!(frame["event"]["data"]["is_error"], true);
                assert!(frame["event"]["data"]["error"]
                    .as_str()
                    .unwrap()
                    .contains("DOXA_CODEX_MIGRATE_APPSERVER=1"));
                break;
            }
        }
        assert_eq!(fs::read(store.thread_path()).unwrap(), thread_before);
        assert_eq!(
            fs::read(store.transcript_path()).unwrap(),
            transcript_before
        );
    }
    send(
        &mut socket,
        json!({"type":"call","id":2,"method":"stop","params":{}}),
    );
    while receive(&mut reader)["id"] != 2 {}
    assert!(child.wait().unwrap().success());
    assert!(!root.join("provider-ran").exists());
    assert_eq!(fs::read(store.thread_path()).unwrap(), thread_before);
    assert_eq!(
        fs::read(store.transcript_path()).unwrap(),
        transcript_before
    );
}
#[test]
fn explicit_legacy_migration_resumes_exact_provider_thread_without_creating_one() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let store = seed(root);
    let before = fs::read(store.transcript_path()).unwrap();
    let provider = root.join("provider");
    fs::write(&provider,r#"#!/usr/bin/python3
import json,sys,tomllib
from pathlib import Path
def read():
 line=sys.stdin.readline()
 if not line:sys.exit(0)
 with Path('requests.jsonl').open('a') as stream:stream.write(line)
 return json.loads(line)
def reply(query,result):print(json.dumps({'id':query['id'],'result':result}),flush=True)
def event(method,**params):print(json.dumps({'method':method,'params':dict(threadId='legacy-thread',**params)}),flush=True)
query=read();assert query['method']=='initialize'
reply(query,{'userAgent':'doxa_codex_rs/0.156.1 (doxa-precompact-fail-closed-v1; fixture)'})
assert read()['method']=='initialized'
args=sys.argv[1:];overrides=[args[i+1] for i,value in enumerate(args[:-1]) if value=='-c']
hooks=next(tomllib.loads(value)['hooks'] for value in overrides if value.startswith('hooks='))
query=read();assert query['method']=='config/read';reply(query,{'config':{'features':{'token_budget':False}}})
query=read();assert query['method']=='hooks/list'
key=next(iter(hooks['state']));handler=hooks['PreCompact'][0]['hooks'][0]
reply(query,{'data':[{'hooks':[dict(key=key,command=handler['command'],handlerType='command',enabled=True,trustStatus='trusted',currentHash=hooks['state'][key]['trusted_hash'],eventName='preCompact',source='sessionFlags',timeoutSec=240,**{'async':False})]}]})
query=read();assert query['method']=='thread/resume' and query['params']['threadId']=='legacy-thread'
reply(query,{'thread':{'id':'legacy-thread'},'model':'gpt-5.5'})
query=read();assert query['method']=='turn/start' and query['params']['threadId']=='legacy-thread'
reply(query,{'turn':{'id':'migration-turn'}})
event('item/completed',turnId='migration-turn',item={'id':'answer','type':'agentMessage','text':'native continuation'})
event('turn/completed',turn={'id':'migration-turn','status':'completed','error':None})
sys.stdin.read()
"#).unwrap();
    fs::set_permissions(&provider, fs::Permissions::from_mode(0o700)).unwrap();
    let mut child = command(root, &provider)
        .env("DOXA_CODEX_MIGRATE_APPSERVER", "1")
        .spawn()
        .unwrap();
    let (mut reader, mut socket) = connect(root);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(
        &mut socket,
        json!({"type":"prompt","id":1,"text":"continue the saved thread"}),
    );
    loop {
        let frame = receive(&mut reader);
        if frame["event"]["type"] == "turn_done" {
            assert_eq!(frame["event"]["data"]["is_error"], false, "{frame}");
            break;
        }
    }
    send(
        &mut socket,
        json!({"type":"call","id":2,"method":"stop","params":{}}),
    );
    while receive(&mut reader)["id"] != 2 {}
    assert!(child.wait().unwrap().success());
    let requests = fs::read_to_string(root.join("requests.jsonl")).unwrap();
    assert!(requests.contains("thread/resume"));
    assert!(!requests.contains("thread/start"));
    let metadata: Value = serde_json::from_slice(&fs::read(store.thread_path()).unwrap()).unwrap();
    assert_eq!(metadata["thread_id"], "legacy-thread");
    assert_eq!(metadata["transport"], "app-server");
    assert_eq!(metadata["opaque_legacy_field"], "retained");
    assert!(fs::read(store.transcript_path())
        .unwrap()
        .starts_with(&before));
}

#![cfg(unix)]

use doxa_lore::{LoreClient, LoreError, MAX_FRAME_BYTES};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::thread;
use std::time::{Duration, Instant};

fn fake(dir: &Path, body: &str) -> std::path::PathBuf {
    let path = dir.join("fake-sidecar");
    fs::write(&path, format!("#!/usr/bin/env python3\n{body}\n")).unwrap();
    let mut perms = fs::metadata(&path).unwrap().permissions();
    perms.set_mode(0o700);
    fs::set_permissions(&path, perms).unwrap();
    path
}

#[test]
fn transient_busy_interpreter_is_retried() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(
        dir.path(),
        r#"
import json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'text':req['text']}), flush=True)
"#,
    );
    let writer = fs::OpenOptions::new().write(true).open(&path).unwrap();
    let release = thread::spawn(move || {
        thread::sleep(Duration::from_millis(30));
        drop(writer);
    });
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    release.join().unwrap();
    assert_eq!(client.scrub("safe").unwrap(), "safe");
}

#[test]
fn fake_sidecar_scrubs_and_snapshots_with_in_order_ids() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(
        dir.path(),
        r#"
import json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    text = req['text'].replace('SECRET', '[redacted]') if req['op'] == 'scrub' else 'memory for ' + req['scope']
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'text':text}), flush=True)
"#,
    );
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    assert_eq!(client.scrub("token SECRET").unwrap(), "token [redacted]");
    assert_eq!(
        client.snapshot("/repo", "project").unwrap(),
        "memory for project"
    );
    assert!(matches!(
        client.snapshot("/repo", "invalid"),
        Err(LoreError::InvalidFrame)
    ));
}

#[test]
fn unavailable_or_malformed_capabilities_fail_at_handshake() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(
        dir.path(),
        "print('{\"type\":\"hello\",\"proto\":1,\"capabilities\":[]}', flush=True)",
    );
    assert!(matches!(
        LoreClient::spawn(&path, Duration::from_secs(2)),
        Err(LoreError::Unavailable)
    ));
}

#[test]
fn blocked_stdin_and_silent_sidecar_obey_deadline() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(
        dir.path(),
        r#"
import time
print('{"type":"hello","proto":1,"capabilities":["scrub","snapshot"]}', flush=True)
time.sleep(30)
"#,
    );
    let mut client = LoreClient::spawn(&path, Duration::from_millis(120)).unwrap();
    let started = Instant::now();
    assert!(matches!(
        client.scrub(&"x".repeat(MAX_FRAME_BYTES - 100)),
        Err(LoreError::Timeout)
    ));
    assert!(started.elapsed() < Duration::from_secs(2));
}

#[test]
fn oversized_or_wrong_id_reply_is_not_accepted() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(
        dir.path(),
        r#"
import sys
print('{"type":"hello","proto":1,"capabilities":["scrub","snapshot"]}', flush=True)
sys.stdin.readline()
print('{"type":"reply","id":999,"ok":true,"text":"wrong"}', flush=True)
"#,
    );
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    assert!(matches!(
        client.scrub("hello"),
        Err(LoreError::InvalidFrame)
    ));
    assert!(matches!(client.scrub("again"), Err(LoreError::Closed)));
}

#[test]
fn typed_lore_readers_and_disabled_status() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(
        dir.path(),
        r#"
import json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','pending','sync_state','refresh_interval']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    values = {'pending': [{'pid':'one','text':'[redacted]'}], 'sync_state': None, 'refresh_interval': 30}
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':values[req['op']]}), flush=True)
"#,
    );
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    assert_eq!(client.pending("/repo", 0, 50).unwrap()[0]["pid"], "one");
    assert!(client.sync_state().unwrap().is_none());
    assert_eq!(client.refresh_interval().unwrap(), Some(30));
    assert!(matches!(
        client.pending("/repo", 0, 51),
        Err(LoreError::InvalidFrame)
    ));
}

#[test]
fn older_sidecar_keeps_core_operations_but_disables_new_ones() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(dir.path(), "print('{\"type\":\"hello\",\"proto\":1,\"capabilities\":[\"scrub\",\"snapshot\"]}', flush=True)");
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    assert!(matches!(client.sync_state(), Err(LoreError::Unavailable)));
    assert!(matches!(
        client.consult("query"),
        Err(LoreError::Unavailable)
    ));
}

#[test]
fn typed_read_operations_require_capabilities_and_validate_replies() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(
        dir.path(),
        r#"
import json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','consult','beliefs','evidence']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    values = {
        'consult': {'id':1,'claim':'safe','claim_truncated':False,'confidence':0.8,'score':-1.0,'citation_status':'cite_only'},
        'beliefs': [{'id':1,'subject':'user','claim':'safe','claim_truncated':False,'confidence':0.8,'evidence_count':2}],
        'evidence': [{'session_id':'s','project':'p','note':'safe','note_truncated':False,'created':'2026'}],
    }
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':values[req['op']]}), flush=True)
"#,
    );
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    assert_eq!(client.consult("query").unwrap().unwrap().claim, "safe");
    assert_eq!(client.beliefs(0, 1).unwrap()[0]["evidence_count"], 2);
    assert_eq!(client.evidence(1, 1).unwrap()[0]["note"], "safe");
    assert!(matches!(client.consult(""), Err(LoreError::InvalidFrame)));
    assert!(matches!(
        client.beliefs(0, 51),
        Err(LoreError::InvalidFrame)
    ));
    assert!(matches!(
        client.evidence(0, 1),
        Err(LoreError::InvalidFrame)
    ));
}

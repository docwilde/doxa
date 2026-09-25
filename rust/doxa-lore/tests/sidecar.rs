#![cfg(unix)]

use doxa_lore::{BeliefAction, BeliefStatus, LoreClient, LoreError, MemoryUsage, PendingDecision, PendingResolution, MAX_FRAME_BYTES};
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
fn belief_action_requires_review_capability_and_reports_retirement() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(dir.path(), r#"
import json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','belief_review_v1','belief_action_v1']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if req['op'] == 'belief_review_v1':
        value = {'id':req['belief_id'],'uid':'uid-1','subject':'project:repo','claim':'safe fact','claim_sha256':'a'*64}
    else:
        assert req['expected'] == {'uid':'uid-1','subject':'project:repo','claim_sha256':'a'*64}
        value = {'status':'dormant','retired':True,'confirmed':0,'contradicted':2,'stale':0}
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"#);
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    assert!(client.can_act_on_beliefs());
    let review = client.belief_review("/repo", 7).unwrap();
    assert_eq!(review.id(), 7);
    assert_eq!(review.claim(), "safe fact");
    assert_eq!(review.subject(), "project:repo");
    let result = client.belief_action("/repo", &review, BeliefAction::Contradicted, "failed check").unwrap();
    assert_eq!(result.status, BeliefStatus::Dormant);
    assert!(result.retired);
    assert_eq!(result.contradicted, 2);
    assert!(matches!(client.belief_action("/repo", &review, BeliefAction::Stale, ""), Err(LoreError::InvalidFrame)));

    let old = fake(dir.path(), "print('{\"type\":\"hello\",\"proto\":1,\"capabilities\":[\"scrub\",\"snapshot\"]}', flush=True)");
    let mut older = LoreClient::spawn(&old, Duration::from_secs(2)).unwrap();
    assert!(!older.can_act_on_beliefs());
    assert!(matches!(older.belief_review("/repo", 7), Err(LoreError::Unavailable)));
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
fn memory_usage_requires_capability_and_valid_bounded_counts() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(dir.path(), r#"
import json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','memory_usage_v1']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    values = {
        '/repo': {'project_chars': 7, 'user_chars': 12, 'project_cap_chars': 8800, 'user_cap_chars': 9000},
        '/missing': {'project_chars': 1},
        '/negative': {'project_chars': -1, 'user_chars': 12, 'project_cap_chars': 8800, 'user_cap_chars': 9000},
        '/boolean': {'project_chars': True, 'user_chars': 12, 'project_cap_chars': 8800, 'user_cap_chars': 9000},
        '/large': {'project_chars': 1048577, 'user_chars': 12, 'project_cap_chars': 8800, 'user_cap_chars': 9000},
        '/zero-cap': {'project_chars': 7, 'user_chars': 12, 'project_cap_chars': 0, 'user_cap_chars': 9000},
        '/bad-cap': {'project_chars': 7, 'user_chars': 12, 'project_cap_chars': True, 'user_cap_chars': 9000},
        '/large-cap': {'project_chars': 7, 'user_chars': 12, 'project_cap_chars': 8800, 'user_cap_chars': 1048577},
    }
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':values[req['cwd']]}), flush=True)
"#);
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    assert_eq!(client.memory_usage("/repo").unwrap(), MemoryUsage {
        project_chars: 7, user_chars: 12, project_cap_chars: 8800, user_cap_chars: 9000,
    });
    for cwd in ["/missing", "/negative", "/boolean", "/large", "/zero-cap", "/bad-cap", "/large-cap", ""] {
        assert!(matches!(client.memory_usage(cwd), Err(LoreError::InvalidFrame)), "{cwd}");
    }
    let old = fake(dir.path(), "print('{\"type\":\"hello\",\"proto\":1,\"capabilities\":[\"scrub\",\"snapshot\"]}', flush=True)");
    let mut older = LoreClient::spawn(&old, Duration::from_secs(2)).unwrap();
    assert!(matches!(older.memory_usage("/repo"), Err(LoreError::Unavailable)));
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
    assert!(matches!(
        client.pending_review("/repo", "one"),
        Err(LoreError::Unavailable)
    ));
    assert!(matches!(
        client.index_transcript("/repo", "session-1"),
        Err(LoreError::Unavailable)
    ));
}

#[test]
fn index_transcript_uses_capability_and_session_identity() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(
        dir.path(),
        r#"
import json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','index_transcript_v1']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    assert req['op'] == 'index_transcript_v1'
    assert set(req) == {'id', 'op', 'cwd', 'session_id'}
    value = {'indexed': 2, 'consumed': 3} if req['session_id'] == 'session-1' else {'indexed': 4, 'consumed': 3}
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"#,
    );
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    assert_eq!(client.index_transcript("/repo", "session-1").unwrap(), 2);
    for session_id in ["", "../secret", "with_underscore", "é", "-bad"] {
        assert!(matches!(
            client.index_transcript("/repo", session_id),
            Err(LoreError::InvalidFrame)
        ));
    }
    assert!(matches!(
        client.index_transcript("/repo", "session-2"),
        Err(LoreError::InvalidFrame)
    ));
}

#[test]
fn full_review_binds_complete_raw_bytes_and_inode() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(
        dir.path(),
        r#"
import hashlib, json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','pending_review_v1']}), flush=True)
raw = '{"kind":"sync","op":{"payload":"entire signed op bytes"}}\n'
for line in sys.stdin:
    req = json.loads(line)
    if req['pid'] == 'mutating' and 'expected' in req:
        print(json.dumps({'type':'reply','id':req['id'],'ok':False,'error':'pending_changed'}), flush=True)
        continue
    value = {'pid':req['pid'], 'raw':raw, 'sha256':hashlib.sha256(raw.encode()).hexdigest(), 'inode':87, 'complete':True}
    if req['pid'] == 'changed': value['sha256'] = '0' * 64
    if req['pid'] == 'partial': value['complete'] = False
    if req['pid'] == 'swapped': value['pid'] = 'other'
    if req['pid'] == 'invalid': value['raw'] = '{"kind":'
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"#,
    );
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    let review = client.pending_review("/repo", "one").unwrap();
    assert_eq!(review.pid(), "one");
    assert_eq!(review.inode(), 87);
    assert!(review.raw().contains("entire signed op bytes"));
    assert_eq!(review.sha256().len(), 64);
    assert_eq!(
        client
            .pending_review_if_unchanged("/repo", &review)
            .unwrap(),
        review
    );
    let mutating = client.pending_review("/repo", "mutating").unwrap();
    assert!(matches!(
        client.pending_review_if_unchanged("/repo", &mutating),
        Err(LoreError::Remote("pending_changed"))
    ));
    for pid in ["changed", "partial", "swapped", "invalid"] {
        assert!(
            matches!(
                client.pending_review("/repo", pid),
                Err(LoreError::InvalidFrame)
            ),
            "{pid}"
        );
    }
    for pid in ["../other", "", "a/b"] {
        assert!(
            matches!(
                client.pending_review("/repo", pid),
                Err(LoreError::InvalidFrame)
            ),
            "{pid}"
        );
    }
}

#[test]
fn reviewed_resolution_is_one_snapshot_and_reports_partial_archive() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(dir.path(), r#"
import hashlib, json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','pending_review_v1','resolve_reviewed_v1']}), flush=True)
raw = '{"kind":"memory","text":"reviewed"}\n'
for line in sys.stdin:
    req = json.loads(line)
    if req['op'] == 'pending_review_v1':
        value = {'pid':req['pid'],'raw':raw,'sha256':hashlib.sha256(raw.encode()).hexdigest(),'inode':17,'complete':True}
    else:
        assert req['op'] == 'resolve_reviewed_v1'
        assert req['cwd'] == '/repo' and req['pid'] == 'one'
        assert req['expected'] == {'sha256':hashlib.sha256(raw.encode()).hexdigest(),'inode':17}
        if req['decision'] == 'approve':
            value = {'status':'refused','error':'archive_failed','applied':True}
        else:
            value = {'status':'rejected'}
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"#);
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    let review = client.pending_review("/repo", "one").unwrap();
    assert_eq!(client.resolve_reviewed("/repo", &review, PendingDecision::Reject).unwrap(), PendingResolution::Rejected);
    assert_eq!(client.resolve_reviewed("/repo", &review, PendingDecision::Approve).unwrap(),
        PendingResolution::Refused { code: "archive_failed".into(), applied: true });
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

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
fn session_search_requires_capability_and_checks_bounded_hit_identity() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(dir.path(), r#"
import json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','session_search_v1']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    assert req['op'] == 'session_search_v1'
    value = [{'session_id':'saved-1','project':'repo','snippet':'[needle]'}]
    if req['query'] == 'bad': value[0]['project'] = '../escape'
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"#);
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    assert_eq!(client.session_search("/repo", "needle").unwrap()[0].session_id, "saved-1");
    assert!(matches!(client.session_search("/repo", "bad"), Err(LoreError::InvalidFrame)));
    assert!(matches!(client.session_search("/repo", "\n"), Err(LoreError::InvalidFrame)));
    let old = fake(dir.path(), "print('{\"type\":\"hello\",\"proto\":1,\"capabilities\":[\"scrub\",\"snapshot\"]}', flush=True)");
    let mut older = LoreClient::spawn(&old, Duration::from_secs(2)).unwrap();
    assert!(matches!(older.session_search("/repo", "needle"), Err(LoreError::Unavailable)));
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
        if req['belief_id'] == 8:
            print(json.dumps({'type':'reply','id':req['id'],'ok':False,'error':'belief_incomplete'}), flush=True)
            continue
        value = {'id':req['belief_id'],'uid':'uid-1','subject':'project:repo','claim':'safe fact','claim_sha256':'a'*64}
    else:
        assert req['expected'] == {'uid':'uid-1','subject':'project:repo','claim_sha256':'a'*64}
        if req['action'] == 'stale':
            print(json.dumps({'type':'reply','id':req['id'],'ok':False,'error':'belief_changed'}), flush=True)
            continue
        value = {'status':'dormant','retired':True,'confirmed':0,'contradicted':2,'stale':0}
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"#);
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    assert!(client.can_act_on_beliefs());
    let review = client.belief_review("/repo", 7).unwrap();
    assert_eq!(review.id(), 7);
    assert_eq!(review.claim(), "safe fact");
    assert_eq!(review.subject(), "project:repo");
    let debug = format!("{review:?}");
    assert!(!debug.contains("safe fact"));
    assert!(!debug.contains("uid-1"));
    let result = client.belief_action("/repo", &review, BeliefAction::Contradicted, "failed check").unwrap();
    assert_eq!(result.status, BeliefStatus::Dormant);
    assert!(result.retired);
    assert_eq!(result.contradicted, 2);
    assert!(matches!(client.belief_action("/repo", &review, BeliefAction::Stale, ""), Err(LoreError::InvalidFrame)));
    assert!(matches!(client.belief_action("/repo", &review, BeliefAction::Stale, "no longer applies"),
        Err(LoreError::Remote("belief_changed"))));
    assert!(matches!(client.belief_review("/repo", 8), Err(LoreError::Remote("belief_incomplete"))));

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
    assert_eq!(client.memory_usage("/zero-cap").unwrap().project_cap_chars, 0);
    for cwd in ["/missing", "/negative", "/boolean", "/large", "/bad-cap", "/large-cap", ""] {
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
fn reviewed_resolution_is_one_snapshot_and_preserves_indeterminate_archive() {
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
            approvals = globals().get('approvals', 0) + 1
            if approvals == 1:
                value = {'status':'refused','error':'operation_failed','applied':True}
            else:
                value = {'status':'refused','error':'operation_failed','applied':None,'may_have_applied':True}
        else:
            value = {'status':'rejected'}
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"#);
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    let review = client.pending_review("/repo", "one").unwrap();
    assert_eq!(client.resolve_reviewed("/repo", &review, PendingDecision::Reject).unwrap(), PendingResolution::Rejected);
    assert_eq!(client.resolve_reviewed("/repo", &review, PendingDecision::Approve).unwrap(),
        PendingResolution::Refused { code: "operation_failed".into(), applied: true });
    assert_eq!(client.resolve_reviewed("/repo", &review, PendingDecision::Approve).unwrap(),
        PendingResolution::Indeterminate { code: "operation_failed".into() });
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

#[test]
fn filtered_beliefs_forward_query_and_refuse_unadvertised_filtering() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(dir.path(), r#"
import json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','beliefs','beliefs_filtered_v1']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    assert req['op'] == 'beliefs_filtered_v1' and req['offset'] == 20 and req['limit'] == 1
    value = [{'id':4,'subject':'user','claim':'Straße','claim_truncated':False,'confidence':0.8,'evidence_count':1,
              'updated':None,'created':'2026-01-01T00:00:00Z','recency':'2026-01-01T00:00:00Z'}]
    if req['query'] == 'bad': value[0]['recency'] = 42
    else: assert req['query'] == 'strasse'
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"#);
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    assert_eq!(client.beliefs_filtered(20, 1, "strasse").unwrap()[0]["id"], 4);
    assert!(matches!(client.beliefs_filtered(20, 1, "bad"), Err(LoreError::InvalidFrame)));
    assert!(matches!(client.beliefs_filtered(20, 1, "line\nbreak"), Err(LoreError::InvalidFrame)));
    let old = fake(dir.path(), "print('{\"type\":\"hello\",\"proto\":1,\"capabilities\":[\"scrub\",\"snapshot\",\"beliefs\"]}', flush=True)");
    let mut older = LoreClient::spawn(&old, Duration::from_secs(2)).unwrap();
    assert!(matches!(older.beliefs_filtered(20, 1, "strasse"), Err(LoreError::Unavailable)));
}

#[test]
fn belief_display_is_bounded_read_only_and_checks_reply_identity_and_completeness() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(dir.path(), r#"
import json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','belief_display_v1']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    assert req['op'] == 'belief_display_v1' and req['cwd'] == '/current-checkout'
    assert 'expected' not in req
    bid = req['belief_id']
    value = {'id':bid,'subject':'project:another-checkout','claim':'full\n' + 'ü'*5000,'complete':True,'redacted':False}
    if bid == 2: value.update(claim='[redacted]', complete=False, redacted=True)
    if bid == 3: value['id'] = 999
    if bid == 4: value.update(complete=True, redacted=True)
    if bid == 5: value['claim'] = 'unsafe\x1b[31m'
    if bid == 6: value['claim'] = 'ü'*32769
    if bid == 7: value['claim_sha256'] = 'a'*64
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"#);
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    let full = client.belief_display("/current-checkout", 1).unwrap();
    assert!(full["claim"].as_str().unwrap().contains('\n'));
    assert!(full["claim"].as_str().unwrap().len() > 4096);
    assert_eq!(full["complete"], true);
    assert_eq!(client.belief_display("/current-checkout", 2).unwrap()["complete"], false);
    for bid in 3..=7 {
        assert!(matches!(client.belief_display("/current-checkout", bid), Err(LoreError::InvalidFrame)));
    }
    assert!(matches!(client.belief_display("/current-checkout", 0), Err(LoreError::InvalidFrame)));
    let old = fake(dir.path(), "print('{\"type\":\"hello\",\"proto\":1,\"capabilities\":[\"scrub\",\"snapshot\"]}', flush=True)");
    let mut older = LoreClient::spawn(&old, Duration::from_secs(2)).unwrap();
    assert!(matches!(older.belief_display("/current-checkout", 1), Err(LoreError::Unavailable)));
}

#[test]
fn curated_entry_reads_validate_complete_rows_and_require_canonical_capability() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(dir.path(), r#"
import json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','memory_entries_v1']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    assert req['op'] == 'memory_entries_v1' and req['scope'] == 'user'
    value = [{'text':'individual fact','source':'codex','redacted':False},
             {'text':'[redacted] fact','source':None,'redacted':True}]
    if req['cwd'] == '/control': value[0]['text'] = 'fact\x1b'
    if req['cwd'] == '/many': value = [value[0]] * 401
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"#);
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    let rows = client.memory_entries("/repo", "user").unwrap();
    assert_eq!(rows[0]["source"], "codex");
    assert_eq!(rows[1]["redacted"], true);
    for cwd in ["/control", "/many"] {
        assert!(matches!(client.memory_entries(cwd, "user"), Err(LoreError::InvalidFrame)));
    }
    assert!(matches!(client.memory_entries("/repo", "all"), Err(LoreError::InvalidFrame)));
    let old = fake(dir.path(), "print('{\"type\":\"hello\",\"proto\":1,\"capabilities\":[\"scrub\",\"snapshot\"]}', flush=True)");
    let mut older = LoreClient::spawn(&old, Duration::from_secs(2)).unwrap();
    assert!(matches!(older.memory_entries("/repo", "user"), Err(LoreError::Unavailable)));
}

#[test]
fn curated_review_checks_exact_unicode_chars_digest_and_rejects_malformed_action() {
    let dir = tempfile::tempdir().unwrap();
    let path = fake(dir.path(), r#"
import hashlib, json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','memory_review_v1','memory_action_v1']}), flush=True)
body = '- ü fact\n'
for line in sys.stdin:
    req = json.loads(line)
    if req['op'] == 'memory_review_v1':
        value = {'scope':req['scope'],'key':'user','entries':['ü fact'], 'chars':len(body),'cap_chars':9000,'sha256':hashlib.sha256(body.encode()).hexdigest()}
        if req['cwd'] == '/wrong': value['chars'] = len(body.encode())
        if req['cwd'] == '/changed': value['entries'] = ['changed fact']
    else:
        assert req['expected']['key'] == 'user'
        assert req['expected']['sha256'] == hashlib.sha256(body.encode()).hexdigest()
        assert req['scope'] == 'user' and req['action'] == 'remove'
        value = {'status':'staged'}
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"#);
    let mut client = LoreClient::spawn(&path, Duration::from_secs(2)).unwrap();
    let review = client.memory_review("/repo", "user").unwrap();
    assert_eq!(review["entries"][0], "ü fact");
    for cwd in ["/wrong", "/changed"] {
        assert!(matches!(client.memory_review(cwd, "user"), Err(LoreError::InvalidFrame)));
    }
    assert!(matches!(client.memory_action("/repo", serde_json::json!({"scope":"user","action":"remove", "text":"", "entry":"ü fact", "expected":{"key":"user","sha256":"wrong"}})), Err(LoreError::InvalidFrame)));
    let result = client.memory_action("/repo", serde_json::json!({"scope":"user","action":"remove", "text":"", "entry":"ü fact", "expected":{"key":"user","sha256":review["sha256"]}})).unwrap();
    assert_eq!(result["status"], "staged");
}

// SPDX-License-Identifier: AGPL-3.0-only
use doxa_peers::{now, PeerRecord, Registry, MAX_ENTRY_BYTES};
use std::fs;
use std::os::unix::fs::{symlink, PermissionsExt};
use std::os::unix::net::UnixListener;
use std::path::Path;

fn scrub(s: &str) -> String { s.replace("SECRET", "[REDACTED]") }
fn record(id: &str, socket: &Path) -> PeerRecord {
    PeerRecord { session_id:id.into(), pid:std::process::id() as i32,
        socket_path:socket.to_string_lossy().into_owned(), cwd:"/work/project".into(), repo_root:Some("/work".into()),
        title:"SECRET title".into(), started_at:now(), heartbeat_at:now(), daemon_socket:None,
        clients:Some(0), usage_tokens:Some(17), provider:Some("claude".into()), model:None, engine:Some("doxa".into()), parent_session_id:None }
}
#[test]
fn python_schema_fixture_and_unknown_fields() {
    let fixture = include_str!("fixtures/python_peer.json");
    let p: PeerRecord = serde_json::from_str(fixture).unwrap();
    assert_eq!(p.scope_key(), "/work/project");
    assert_eq!(p.clients, Some(0));
    assert_eq!(p.usage_tokens, Some(1234));
    assert_eq!(p.model.as_deref(), Some("sonnet"));
    let mut v: serde_json::Value = serde_json::from_str(fixture).unwrap();
    v["future_field"] = true.into();
    assert_eq!(serde_json::from_value::<PeerRecord>(v).unwrap(), p);
    let old = serde_json::json!({"session_id":"old","pid":1,"socket_path":"/x","cwd":"/work","repo_root":null,"title":"old","started_at":"2026-09-24T10:00:00.000000Z","heartbeat_at":"2026-09-24T10:00:01.000000Z"});
    assert_eq!(serde_json::from_value::<PeerRecord>(old).unwrap().provider, None);
    assert!(serde_json::to_value(&p).unwrap().get("origin").is_none());
}
#[test]
fn private_atomic_records_scope_and_scrub() {
    let tmp = tempfile::tempdir().unwrap();
    let rt = tmp.path().join("runtime");
    let reg = Registry::open(&rt).unwrap();
    assert_eq!(fs::metadata(&rt).unwrap().permissions().mode() & 0o777, 0o700);
    assert_eq!(fs::metadata(reg.directory()).unwrap().permissions().mode() & 0o777, 0o700);
    let sock = rt.join("peer-live.sock"); let _listener = UnixListener::bind(&sock).unwrap();
    let mut p = record("live-1", &sock);
    reg.write(&p).unwrap();
    let path = reg.directory().join("live-1.json");
    assert_eq!(fs::metadata(path).unwrap().permissions().mode() & 0o777, 0o600);
    let peers = reg.scoped("/work", None, &scrub, true).unwrap();
    assert_eq!(peers.len(), 1);
    assert_eq!(peers[0].title, "[REDACTED] title");
    assert!(reg.scoped("/other", None, &scrub, true).unwrap().is_empty());
    assert!(reg.scoped("/work", Some("live-1"), &scrub, true).unwrap().is_empty());
    p.usage_tokens = Some(18); reg.heartbeat(&mut p).unwrap();
    assert_eq!(reg.read(&scrub, false, false).unwrap()[0].usage_tokens, Some(18));
}
#[test]
fn bounded_reads_and_symlink_rejection() {
    let tmp = tempfile::tempdir().unwrap(); let reg = Registry::open(tmp.path().join("rt")).unwrap();
    let huge = reg.directory().join("huge.json"); fs::write(&huge, vec![b'x'; MAX_ENTRY_BYTES as usize + 1]).unwrap();
    let victim = tmp.path().join("victim"); fs::write(&victim, "keep").unwrap();
    let link = reg.directory().join("link.json"); symlink(&victim, &link).unwrap();
    assert!(reg.read(&scrub, false, false).unwrap().is_empty());
    assert_eq!(fs::read_to_string(&victim).unwrap(), "keep");
    for id in ["-bad", "_bad", "a_b", "../bad"] {
        assert!(reg.write(&record(id, &victim)).is_err());
    }
}
#[test]
fn stale_live_pid_keeps_socket_dead_pid_only_removes_contained_socket() {
    let tmp=tempfile::tempdir().unwrap(); let rt=tmp.path().join("rt"); let reg=Registry::open(&rt).unwrap();
    let sock=rt.join("peer-suspended.sock"); let _listener=UnixListener::bind(&sock).unwrap();
    let mut p=record("suspended",&sock); p.heartbeat_at="2020-01-01T00:00:00.000000Z".into();
    reg.write(&p).unwrap();
    assert!(reg.read(&scrub,true,false).unwrap().is_empty());
    assert!(sock.exists());
    let outside=tmp.path().join("outside.sock"); let _outside=UnixListener::bind(&outside).unwrap();
    let mut dead=record("dead",&outside); dead.pid=i32::MAX; reg.write(&dead).unwrap();
    assert!(reg.read(&scrub,true,false).unwrap().is_empty());
    assert!(outside.exists());
    let reused=rt.join("peer-reused.sock"); let _reused_listener=UnixListener::bind(&reused).unwrap();
    dead.session_id="dead-reused".into(); dead.socket_path=reused.to_string_lossy().into_owned(); reg.write(&dead).unwrap();
    assert!(reg.read(&scrub,true,false).unwrap().is_empty());
    assert!(reused.exists(), "a live listener must survive a forged or recycled dead PID");
    let inner=rt.join("peer-dead.sock"); let inner_listener=UnixListener::bind(&inner).unwrap();
    drop(inner_listener);
    dead.session_id="dead-inner".into(); dead.socket_path=inner.to_string_lossy().into_owned(); reg.write(&dead).unwrap();
    assert!(reg.read(&scrub,true,false).unwrap().is_empty());
    assert!(!inner.exists());
}
#[test]
fn launch_sweep_removes_unreachable_presence_without_touching_live_pid_socket() {
    let tmp=tempfile::tempdir().unwrap(); let rt=tmp.path().join("rt"); let reg=Registry::open(&rt).unwrap();
    let missing=rt.join("peer-missing.sock");
    reg.write(&record("unreachable",&missing)).unwrap();
    assert_eq!(reg.sweep_stale(&scrub).unwrap(),1);
    assert!(!reg.directory().join("unreachable.json").exists());
}

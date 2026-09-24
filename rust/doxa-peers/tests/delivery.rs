// SPDX-License-Identifier: AGPL-3.0-only
use doxa_peers::{delivery::*, now, PeerRecord, Registry};
use std::fs;
use std::io;
use std::os::unix::fs::{symlink, PermissionsExt};
use std::path::Path;
use std::thread;
use std::time::Duration;

fn peer(id: &str, socket: &Path, scope: &str) -> PeerRecord {
    PeerRecord { session_id: id.into(), pid: std::process::id() as i32,
        socket_path: socket.display().to_string(), cwd: scope.into(), repo_root: None,
        title: format!("title-{id}"), started_at: now(), heartbeat_at: now(),
        daemon_socket: None, clients: None, usage_tokens: None, provider: None,
        model: Some("test".into()), engine: Some("fake".into()), parent_session_id: None }
}

#[test]
fn local_delivery_scrubs_receive_and_ledger_but_hashes_raw() -> io::Result<()> {
    let temp = tempfile::tempdir()?;
    let runtime = temp.path().join("runtime");
    let registry = Registry::open(&runtime)?;
    let inbox = Inbox::bind(&runtime, "recipient")?;
    let sender = peer("sender", Path::new("/unused"), "/repo");
    let recipient = peer("recipient", inbox.path(), "/repo");
    registry.write(&recipient)?;
    let worker = thread::spawn(move || inbox.receive(&|text: &str| text.replace("SECRET", "[redacted]")));
    let ledger_path = temp.path().join("peers/messages.jsonl");
    let ledger = Ledger::new(ledger_path.clone());
    let mut limiter = RateLimiter::new(SendLimits::default());
    let result = deliver(&registry, &sender, &["recipient".into()], "SECRET", "direct", Some("turn-1"), &mut limiter, &ledger,
        &|text: &str| text.replace("SECRET", "[redacted]"))?;
    let frame = worker.join().unwrap()?;
    assert_eq!(frame.body, "[redacted]");
    assert_eq!(result.delivered, vec!["recipient"]);
    assert!(result.failed.is_empty());
    assert!(result.ledger_error.is_none());
    let line = fs::read_to_string(ledger_path)?;
    assert!(!line.contains("SECRET"));
    let record: Message = serde_json::from_str(line.trim())?;
    assert_eq!(record.body, "[redacted]");
    assert_eq!(record.body_sha256, "0917b13a9091915d54b6336f45909539cce452b3661b21f386418a257883b30a");
    assert_eq!(record.turn.id.as_deref(), Some("turn-1"));
    Ok(())
}

#[test]
fn refuses_cross_scope_before_charge_and_stale_socket_is_not_removed() -> io::Result<()> {
    let temp = tempfile::tempdir()?;
    let runtime = temp.path().join("runtime");
    let registry = Registry::open(&runtime)?;
    let inbox = Inbox::bind(&runtime, "recipient")?;
    registry.write(&peer("recipient", inbox.path(), "/elsewhere"))?;
    let ledger = Ledger::new(temp.path().join("peers/messages.jsonl"));
    let mut limiter = RateLimiter::new(SendLimits { per_turn: 1, per_window: 1, window: Duration::from_secs(60) });
    let sender = peer("sender", Path::new("/unused"), "/repo");
    assert!(deliver(&registry, &sender, &["recipient".into()], "hello", "direct", Some("t"), &mut limiter, &ledger, &|s: &str| s.to_owned()).is_err());
    assert!(inbox.path().exists());
    // A refused target did not consume the sender's budget.
    let live = Inbox::bind(&runtime, "live")?;
    registry.write(&peer("live", live.path(), "/repo"))?;
    let worker = thread::spawn(move || live.receive(&|s: &str| s.to_owned()));
    assert!(deliver(&registry, &sender, &["live".into()], "hello", "direct", Some("t"), &mut limiter, &ledger, &|s: &str| s.to_owned()).is_ok());
    worker.join().unwrap()?;
    Ok(())
}

#[test]
fn rejects_oversize_and_unsafe_paths_and_bounds_budget() -> io::Result<()> {
    let temp = tempfile::tempdir()?;
    let runtime = temp.path().join("runtime");
    fs::create_dir(&runtime)?;
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700))?;
    let inbox = Inbox::bind(&runtime, "recipient")?;
    assert_eq!(Inbox::bind(&runtime, "recipient").err().unwrap().kind(), io::ErrorKind::AlreadyExists);
    let frame = PeerFrame { from_id: "sender".into(), from_title: "test".into(), sent_at: now(), body: "x".repeat(MAX_FRAME_BYTES), from_repo: None, kind: None };
    assert!(send(inbox.path(), &frame).is_err());
    let link = runtime.join("link.sock");
    symlink(inbox.path(), &link)?;
    assert!(send(&link, &frame).is_err());
    let mut limits = RateLimiter::new(SendLimits { per_turn: 2, per_window: 2, window: Duration::from_secs(60) });
    assert!(limits.charge(Some("a"), 2).is_ok());
    assert_eq!(limits.charge(Some("a"), 1).err().unwrap().kind(), io::ErrorKind::WouldBlock);
    assert_eq!(limits.charge(Some("b"), 1).err().unwrap().kind(), io::ErrorKind::WouldBlock);
    Ok(())
}

#[test]
fn ledger_refuses_symlink_and_ceiling() -> io::Result<()> {
    let temp = tempfile::tempdir()?;
    let p = temp.path().join("peers/messages.jsonl");
    fs::create_dir(p.parent().unwrap())?;
    let target = temp.path().join("target");
    fs::write(&target, b"unchanged")?;
    symlink(&target, &p)?;
    let sample = || Message { v: 1, id: "id".into(), ts: now(), sender: Sender { session: "sender".into(), title: None, repo: None, model: None, engine: None },
        to: vec!["recipient".into()], kind: "direct".into(), in_reply_to: None, body: "body".into(), body_sha256: String::new(), latency_ms: None,
        turn: TurnRef { id: None, state: "idle".into() } };
    assert!(Ledger::new(p.clone()).append(sample(), &|s: &str| s.to_owned()).is_err());
    assert_eq!(fs::read(&target)?, b"unchanged");
    fs::remove_file(&p)?;
    assert!(Ledger::with_ceiling(p.clone(), 1).append(sample(), &|s: &str| s.to_owned()).is_err());
    assert_eq!(fs::metadata(&p)?.len(), 0);
    fs::set_permissions(&p, fs::Permissions::from_mode(0o644))?;
    Ledger::new(p.clone()).append(sample(), &|s: &str| s.to_owned())?;
    assert_eq!(fs::metadata(&p)?.permissions().mode() & 0o777, 0o600);
    Ok(())
}

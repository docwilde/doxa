// SPDX-License-Identifier: AGPL-3.0-only
use doxa_peers::{delivery::*, now, PeerRecord, Registry};
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{symlink, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::{mpsc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

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
    let limiter = Mutex::new(RateLimiter::new(SendLimits::default()));
    let result = deliver(&registry, &sender, &["recipient".into()], "SECRET", "direct", Some("turn-1"), &limiter, &ledger,
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
fn polling_idle_peer_does_not_stall_and_retains_partial_frame() -> io::Result<()> {
    let temp = tempfile::tempdir()?;
    let runtime = temp.path().join("runtime");
    let _registry = Registry::open(&runtime)?;
    let inbox = Inbox::bind(&runtime, "recipient")?;
    let mut client = UnixStream::connect(inbox.path())?;
    let started = Instant::now();
    assert!(inbox.poll_receive(&|s: &str| s.to_owned())?.is_none());
    assert!(started.elapsed() < Duration::from_millis(250));
    let frame = PeerFrame { from_id: "sender".into(), from_title: "test".into(),
        sent_at: now(), body: "SECRET".into(), from_repo: None, kind: None };
    let mut bytes = serde_json::to_vec(&frame)?;
    bytes.push(b'\n');
    let midpoint = bytes.len() / 2;
    client.write_all(&bytes[..midpoint])?;
    assert!(inbox.poll_receive(&|s: &str| s.to_owned())?.is_none());
    client.write_all(&bytes[midpoint..])?;
    let received = inbox.poll_receive(&|s: &str| s.replace("SECRET", "[redacted]"))?
        .expect("complete frame after second poll");
    assert_eq!(received.body, "[redacted]");
    Ok(())
}

#[test]
fn blocking_receive_still_waits_after_a_poll() -> io::Result<()> {
    let temp = tempfile::tempdir()?;
    let runtime = temp.path().join("runtime");
    let _registry = Registry::open(&runtime)?;
    let inbox = Inbox::bind(&runtime, "recipient")?;
    assert!(inbox.poll_receive(&|s: &str| s.to_owned())?.is_none());
    let path = inbox.path().to_owned();
    let worker = thread::spawn(move || inbox.receive(&|s: &str| s.to_owned()));
    let frame = PeerFrame { from_id: "sender".into(), from_title: "test".into(),
        sent_at: now(), body: "hello".into(), from_repo: None, kind: None };
    send(&path, &frame)?;
    assert_eq!(worker.join().unwrap()?.body, "hello");
    Ok(())
}

#[test]
fn full_peer_connect_queue_does_not_stall_delivery() -> io::Result<()> {
    let temp = tempfile::tempdir()?;
    let path = temp.path().join("full.sock");
    let listener = UnixListener::bind(&path)?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
    if unsafe { libc::listen(listener.as_raw_fd(), 1) } < 0 { return Err(io::Error::last_os_error()); }
    let bytes = path.as_os_str().as_bytes();
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (slot, byte) in address.sun_path.iter_mut().zip(bytes) { *slot = *byte as libc::c_char; }
    let size = (std::mem::offset_of!(libc::sockaddr_un, sun_path) + bytes.len() + 1) as libc::socklen_t;
    let mut queued = Vec::<OwnedFd>::new();
    let mut full = false;
    for _ in 0..32 {
        let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC, 0) };
        if fd < 0 { return Err(io::Error::last_os_error()); }
        let fd = unsafe { OwnedFd::from_raw_fd(fd) };
        let result = unsafe { libc::connect(fd.as_raw_fd(), (&address as *const libc::sockaddr_un).cast(), size) };
        if result == 0 || io::Error::last_os_error().raw_os_error() == Some(libc::EINPROGRESS) {
            queued.push(fd);
        } else if io::Error::last_os_error().raw_os_error() == Some(libc::EAGAIN) {
            full = true;
            break;
        } else { return Err(io::Error::last_os_error()); }
    }
    assert!(full, "test did not fill the peer listener queue");
    let frame = PeerFrame { from_id: "sender".into(), from_title: "test".into(),
        sent_at: now(), body: "hello".into(), from_repo: None, kind: None };
    let (tx, rx) = mpsc::channel();
    let started = Instant::now();
    let blocked_path = path.clone();
    let blocked_frame = frame.clone();
    thread::spawn(move || { let _ = tx.send(send(&blocked_path, &blocked_frame)); });
    let result = rx.recv_timeout(Duration::from_secs(4)).expect("peer delivery blocked on a full connect queue");
    assert!(result.is_err(), "unaccepted peer should not receive the frame");
    assert!(started.elapsed() < Duration::from_secs(4));

    // A short backlog must recover without losing the next message.
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || { let _ = tx.send(send(&path, &frame)); });
    listener.set_nonblocking(true)?;
    let deadline = Instant::now() + Duration::from_secs(3);
    let mut accepted = Vec::new();
    let recovered = loop {
        if let Ok(result) = rx.try_recv() { break result; }
        match listener.accept() {
            Ok((stream, _)) => accepted.push(stream),
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {},
            Err(error) => return Err(error),
        }
        assert!(Instant::now() < deadline, "peer delivery did not recover after accepting queued peers");
        thread::sleep(Duration::from_millis(10));
    };
    assert!(recovered.is_ok(), "peer delivery did not recover: {recovered:?}");
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
    let limiter = Mutex::new(RateLimiter::new(SendLimits { per_turn: 1, per_window: 1, window: Duration::from_secs(60) }));
    let sender = peer("sender", Path::new("/unused"), "/repo");
    assert!(deliver(&registry, &sender, &["recipient".into()], "hello", "direct", Some("t"), &limiter, &ledger, &|s: &str| s.to_owned()).is_err());
    assert!(inbox.path().exists());
    // A refused target did not consume the sender's budget.
    let live = Inbox::bind(&runtime, "live")?;
    registry.write(&peer("live", live.path(), "/repo"))?;
    let worker = thread::spawn(move || live.receive(&|s: &str| s.to_owned()));
    assert!(deliver(&registry, &sender, &["live".into()], "hello", "direct", Some("t"), &limiter, &ledger, &|s: &str| s.to_owned()).is_ok());
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
    let mut long_turn = RateLimiter::new(SendLimits { per_turn: 2, per_window: 3, window: Duration::from_millis(10) });
    long_turn.charge(Some("long"), 2)?;
    thread::sleep(Duration::from_millis(20));
    long_turn.charge(None, 1)?;
    assert_eq!(long_turn.charge(Some("long"), 1).err().unwrap().kind(), io::ErrorKind::WouldBlock);
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

#[test]
fn ledger_lock_contention_has_a_deadline() -> io::Result<()> {
    let temp = tempfile::tempdir()?;
    let path = temp.path().join("messages.jsonl");
    let held = OpenOptions::new().create(true).write(true).open(&path)?;
    assert_eq!(unsafe { libc::flock(held.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) }, 0);
    let message = Message { v: 1, id: "id".into(), ts: now(),
        sender: Sender { session: "sender".into(), title: None, repo: None, model: None, engine: None },
        to: vec!["recipient".into()], kind: "direct".into(), in_reply_to: None,
        body: "body".into(), body_sha256: String::new(), latency_ms: None,
        turn: TurnRef { id: None, state: "idle".into() } };
    let started = Instant::now();
    let error = Ledger::new(path.clone()).append(message, &|s: &str| s.to_owned()).unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    assert!(started.elapsed() < Duration::from_secs(3));
    assert_eq!(fs::metadata(&path)?.len(), 0);
    Ok(())
}

#[test]
fn broadcast_reply_records_full_fanout_and_refusals_charge_nothing() -> io::Result<()> {
    let temp=tempfile::tempdir()?;let runtime=temp.path().join("runtime");let registry=Registry::open(&runtime)?;
    let first=Inbox::bind(&runtime,"first")?;let second=Inbox::bind(&runtime,"second")?;let foreign=Inbox::bind(&runtime,"foreign")?;
    registry.write(&peer("first",first.path(),"/repo"))?;registry.write(&peer("second",second.path(),"/repo"))?;registry.write(&peer("foreign",foreign.path(),"/other"))?;
    let ledger_path=temp.path().join("peers/messages.jsonl");let ledger=Ledger::new(ledger_path.clone());
    let limiter=Mutex::new(RateLimiter::new(SendLimits{per_turn:2,per_window:2,window:Duration::from_secs(60)}));
    let sender=peer("sender",Path::new("/unused"),"/repo");let clean=|text:&str|text.replace("SECRET","[redacted]");
    let reply="0123456789abcdef0123456789abcdef";
    assert!(deliver_with_reply(&registry,&sender,&["first".into(),"foreign".into()],"SECRET","broadcast",Some("t"),Some(reply),&limiter,&ledger,&clean).is_err());
    assert!(deliver_with_reply(&registry,&sender,&["first".into()],"SECRET","direct",Some("t"),Some("not-a-message"),&limiter,&ledger,&clean).is_err());
    assert!(!ledger_path.exists());
    let first=thread::spawn(move||first.receive(&|text:&str|text.to_owned()));let second=thread::spawn(move||second.receive(&|text:&str|text.to_owned()));
    let result=deliver_with_reply(&registry,&sender,&["first".into(),"second".into()],"SECRET","broadcast",Some("t"),Some(reply),&limiter,&ledger,&clean)?;
    assert_eq!(result.delivered.len(),2);assert!(result.failed.is_empty());
    for worker in [first,second] { let frame=worker.join().unwrap()?;assert_eq!(frame.body,"[redacted]");assert_eq!(frame.kind.as_deref(),Some("broadcast")); }
    assert!(limiter.lock().unwrap().charge(Some("t"),1).is_err());
    let row=result.record.unwrap();assert_eq!(row.in_reply_to.as_deref(),Some(reply));assert_eq!(row.kind,"broadcast");assert_eq!(row.to.len(),2);
    assert!(deliver(&registry,&sender,&["first".into()],"again","direct",Some("t"),&limiter,&ledger,&clean).is_err());
    assert_eq!(fs::read_to_string(ledger_path)?.lines().count(),1);
    assert!(foreign.poll_receive(&clean)?.is_none());
    Ok(())
}

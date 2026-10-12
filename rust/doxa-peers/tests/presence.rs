use doxa_peers::{now, presence::list_scoped_readonly, PeerRecord, Registry};
use std::fs;
use std::io;
use std::os::unix::fs::{symlink, PermissionsExt};
use std::os::unix::net::UnixListener;
use std::path::Path;

fn record(id: &str, socket: &Path, scope: &str, title: &str) -> PeerRecord {
    PeerRecord {
        session_id: id.into(),
        pid: std::process::id() as i32,
        socket_path: socket.to_string_lossy().into_owned(),
        cwd: scope.into(),
        repo_root: None,
        title: title.into(),
        started_at: now(),
        incarnation: None,
        heartbeat_at: now(),
        daemon_socket: None,
        clients: Some(1),
        usage_tokens: None,
        provider: None,
        model: None,
        engine: Some("codex".into()),
        parent_session_id: None,
    }
}

fn listening(runtime: &Path, name: &str) -> UnixListener {
    let path = runtime.join(name);
    let listener = UnixListener::bind(&path).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    listener
}

#[test]
fn same_scope_live_roster_is_bounded_scrubbed_and_read_only() {
    let dir = tempfile::tempdir().unwrap();
    let runtime = dir.path().join("runtime");
    let reg = Registry::open(&runtime).unwrap();
    let _live = listening(&runtime, "live.sock");
    let _other = listening(&runtime, "other.sock");
    reg.write(&record(
        "live",
        &runtime.join("live.sock"),
        "/scope",
        "SECRET builder",
    ))
    .unwrap();
    reg.write(&record(
        "other",
        &runtime.join("other.sock"),
        "/other",
        "SECRET outsider",
    ))
    .unwrap();
    let mut stale = record(
        "stale",
        &runtime.join("missing.sock"),
        "/scope",
        "SECRET stale",
    );
    stale.heartbeat_at = "2020-01-01T00:00:00.000000Z".into();
    reg.write(&stale).unwrap();
    let before = fs::read(reg.directory().join("stale.json")).unwrap();
    let rows = list_scoped_readonly(&runtime, "/scope", "self", |text| {
        Ok(text.replace("SECRET", "[redacted]"))
    })
    .unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].session_id, "live");
    assert_eq!(rows[0].title, "[redacted] builder");
    assert_eq!(
        fs::read(reg.directory().join("stale.json")).unwrap(),
        before
    );
    let json = serde_json::to_value(&rows).unwrap().to_string();
    assert!(!json.contains("/scope") && !json.contains("sock") && !json.contains("pid"));
    assert!(
        list_scoped_readonly(&runtime, "/scope", "live", |s| Ok(s.to_owned()))
            .unwrap()
            .is_empty()
    );
}

#[test]
fn scrub_failure_refuses_result_and_unsafe_targets_are_ignored() {
    let dir = tempfile::tempdir().unwrap();
    let runtime = dir.path().join("runtime");
    let reg = Registry::open(&runtime).unwrap();
    let _listener = listening(&runtime, "live.sock");
    reg.write(&record(
        "live",
        &runtime.join("live.sock"),
        "/scope",
        "SECRET title",
    ))
    .unwrap();
    assert!(
        list_scoped_readonly(&runtime, "/scope", "self", |_| Err(io::Error::other(
            "scrub failed"
        )))
        .is_err()
    );
    let outside = dir.path().join("outside.sock");
    let _outside = UnixListener::bind(&outside).unwrap();
    reg.write(&record("outside", &outside, "/scope", "raw outside"))
        .unwrap();
    symlink(
        reg.directory().join("live.json"),
        reg.directory().join("linked.json"),
    )
    .unwrap();
    let rows = list_scoped_readonly(&runtime, "/scope", "self", |text| {
        Ok(text.replace("SECRET", "redacted"))
    })
    .unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].session_id, "live");
    assert!(
        reg.directory().join("linked.json").exists(),
        "read-only query must not reap symlink"
    );
}

#[test]
fn rejects_public_registry_directory_without_modifying_it() {
    let dir = tempfile::tempdir().unwrap();
    let runtime = dir.path().join("runtime");
    let reg = Registry::open(&runtime).unwrap();
    fs::set_permissions(reg.directory(), fs::Permissions::from_mode(0o777)).unwrap();
    assert!(list_scoped_readonly(&runtime, "/scope", "self", |s| Ok(s.into())).is_err());
    assert_eq!(
        fs::metadata(reg.directory()).unwrap().permissions().mode() & 0o777,
        0o777
    );
}

#[test]
fn roster_and_display_titles_have_fixed_limits() {
    let dir = tempfile::tempdir().unwrap();
    let runtime = dir.path().join("runtime");
    let reg = Registry::open(&runtime).unwrap();
    let mut listeners = Vec::new();
    for index in 0..40 {
        let id = format!("peer-{index:02}");
        let name = format!("{id}.sock");
        listeners.push(listening(&runtime, &name));
        reg.write(&record(
            &id,
            &runtime.join(name),
            "/scope",
            &"x".repeat(200),
        ))
        .unwrap();
    }
    let rows = list_scoped_readonly(&runtime, "/scope", "self", |text| Ok(text.into())).unwrap();
    assert_eq!(rows.len(), 32);
    assert!(rows.iter().all(|row| row.title.chars().count() == 64));
    let all=doxa_peers::presence::list_scoped_readonly_limit(&runtime,"/scope","self",4096,|text|Ok(text.into())).unwrap();
    assert_eq!(all.len(),40);
    assert!(all.iter().any(|row|row.session_id=="peer-39"));
    assert!(doxa_peers::presence::list_scoped_readonly_limit(&runtime,"/scope","self",4097,|text|Ok(text.into())).is_err());
    drop(listeners);
}


#[test]
fn scoped_registry_overflow_is_explicit_and_read_only() {
    let dir = tempfile::tempdir().unwrap();
    let runtime = dir.path().join("runtime");
    let registry = Registry::open(&runtime).unwrap();
    for index in 0..=doxa_peers::MAX_REGISTRY_ENTRIES {
        fs::File::create(registry.directory().join(format!("noise-{index}"))).unwrap();
    }
    let start = std::time::Instant::now();
    assert_eq!(list_scoped_readonly(&runtime, "/scope", "self", |s| Ok(s.to_owned())).unwrap_err().kind(), io::ErrorKind::InvalidData);
    assert!(start.elapsed() < std::time::Duration::from_secs(2));
    assert!(registry.directory().join("noise-0").exists());
}

#[test]
#[cfg(target_os = "linux")]
fn full_backlogs_share_a_roster_deadline_without_reaping_live_records() {
    use std::os::fd::AsRawFd;
    use std::os::unix::ffi::OsStrExt;
    use std::time::{Duration, Instant};
    let dir = tempfile::tempdir().unwrap(); let runtime = dir.path().join("runtime");
    let registry = Registry::open(&runtime).unwrap();
    let mut listeners = Vec::new(); let mut queued = Vec::new();
    for index in 0..25 {
        let id = format!("blocked-{index:02}"); let path = runtime.join(format!("{id}.sock"));
        let listener = listening(&runtime, &format!("{id}.sock"));
        assert_eq!(unsafe { libc::listen(listener.as_raw_fd(), 1) }, 0);
        let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
        let bytes = path.as_os_str().as_bytes(); assert!(bytes.len() < address.sun_path.len());
        address.sun_family = libc::AF_UNIX as libc::sa_family_t;
        for (slot,byte) in address.sun_path.iter_mut().zip(bytes) { *slot = *byte as libc::c_char; }
        let size = (std::mem::offset_of!(libc::sockaddr_un,sun_path)+bytes.len()+1) as libc::socklen_t;
        let mut full = false;
        for _ in 0..32 {
            let fd = doxa_peers::credentials::nonblocking_unix_socket().unwrap();
            if unsafe { libc::connect(fd.as_raw_fd(), (&address as *const libc::sockaddr_un).cast(), size) } == 0 {
                queued.push(fd);
            } else {
                assert_eq!(io::Error::last_os_error().raw_os_error(), Some(libc::EAGAIN));full = true;break;
            }
        }
        assert!(full,"fixture backlog was not full");
        registry.write(&record(&id,&path,"/scope","blocked fixture")).unwrap();listeners.push(listener);
    }
    let saved = fs::read(registry.directory().join("blocked-00.json")).unwrap();
    let started = Instant::now();
    let error = list_scoped_readonly(&runtime,"/scope","self",|text|Ok(text.to_owned())).unwrap_err();
    assert_eq!(error.kind(),io::ErrorKind::TimedOut);
    assert!(started.elapsed()<Duration::from_secs(4),"roster used a separate full deadline for each peer");
    assert_eq!(fs::read(registry.directory().join("blocked-00.json")).unwrap(),saved);
    let started = Instant::now();
    let error = registry.read(&|text:&str|text.to_owned(),true,true).unwrap_err();
    assert_eq!(error.kind(),io::ErrorKind::TimedOut);
    assert!(started.elapsed()<Duration::from_secs(4));
    assert_eq!(fs::read(registry.directory().join("blocked-00.json")).unwrap(),saved);
    drop(queued);drop(listeners);
}

#[test]
fn idle_retained_daemons_are_absent_but_detached_work_and_attached_peers_remain() {
    let dir = tempfile::tempdir().unwrap();
    let runtime = dir.path().join("runtime");
    let reg = Registry::open(&runtime).unwrap();
    let mut listeners = Vec::new();
    let mut snapshots = Vec::new();
    for (id, clients, active) in [
        ("idle", Some(0), None),
        ("busy", Some(0), Some(true)),
        ("attached", Some(1), None),
        ("retired", Some(0), Some(false)),
        ("legacy", None, None),
    ] {
        let socket = runtime.join(format!("{id}.sock"));
        listeners.push(listening(&runtime, &format!("{id}.sock")));
        let mut peer = record(id, &socket, "/scope", id);
        peer.clients = clients;
        let mut value = serde_json::to_value(peer).unwrap();
        if let Some(active) = active { value["active"] = active.into(); }
        let path = reg.directory().join(format!("{id}.json"));
        fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        snapshots.push((path.clone(), fs::read(path).unwrap()));
    }
    let rows = list_scoped_readonly(&runtime, "/scope", "self", |s| Ok(s.into())).unwrap();
    let ids: std::collections::BTreeSet<_> = rows.iter().map(|row| row.session_id.as_str()).collect();
    assert_eq!(ids, std::collections::BTreeSet::from(["attached", "busy", "legacy"]));
    for (path, before) in snapshots { assert_eq!(fs::read(path).unwrap(), before); }
}

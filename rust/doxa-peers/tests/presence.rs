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
        heartbeat_at: now(),
        daemon_socket: None,
        clients: Some(0),
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
    drop(listeners);
}

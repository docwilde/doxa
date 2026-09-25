use doxa_peers::delivery::Ledger;
use doxa_peers::query::MAX_QUERY_RESULTS;
use std::fs;
use std::io;
use std::os::unix::fs::{symlink, PermissionsExt};

fn fixture() -> (tempfile::TempDir, Ledger, std::path::PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let peers = dir.path().join("peers");
    fs::create_dir(&peers).unwrap();
    fs::set_permissions(&peers, fs::Permissions::from_mode(0o700)).unwrap();
    let path = peers.join("messages.jsonl");
    fs::write(&path, include_bytes!("fixtures/python_ledger.jsonl")).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    (dir, Ledger::new(path.clone()), path)
}

#[test]
fn python_query_order_filters_and_cursors() {
    let (_dir, ledger, _path) = fixture();
    let all = ledger.recent(50).unwrap();
    assert_eq!(
        all.iter().map(|m| m.body.as_str()).collect::<Vec<_>>(),
        ["third", "second", "first"]
    );
    assert_eq!(ledger.in_repo("/repo/one", 1).unwrap()[0].body, "second");
    assert_eq!(ledger.sent_by("s-alpha", 50).unwrap()[0].body, "first");
    assert_eq!(ledger.received_by("s-alpha", 50).unwrap()[0].body, "second");
    assert_eq!(
        ledger
            .since(None, None, 50)
            .unwrap()
            .iter()
            .map(|m| m.body.as_str())
            .collect::<Vec<_>>(),
        ["first", "second", "third"]
    );
    assert_eq!(
        ledger
            .since(Some(&"a".repeat(32)), Some("s-alpha"), 50)
            .unwrap()[0]
            .body,
        "second"
    );
    assert_eq!(
        ledger.since(Some(&"b".repeat(32)), None, 1).unwrap()[0].body,
        "third"
    );
    assert!(ledger
        .since(Some(&"c".repeat(32)), None, 50)
        .unwrap()
        .is_empty());
    assert_eq!(ledger.latest_id().unwrap(), Some("c".repeat(32)));
    assert_eq!(ledger.query_stats().unwrap().count, 3);
    assert_eq!(
        ledger.since(Some("unknown"), None, 50).unwrap_err().kind(),
        io::ErrorKind::NotFound
    );
    assert!(ledger.recent(MAX_QUERY_RESULTS + 1).is_err());
}

#[test]
fn partial_and_malformed_lines_do_not_advance_cursor() {
    let (_dir, ledger, path) = fixture();
    let mut bytes = fs::read(&path).unwrap();
    bytes.extend_from_slice(b"broken\n{\"id\":\"partial\"");
    fs::write(&path, bytes).unwrap();
    let stats = ledger.query_stats().unwrap();
    assert_eq!(stats.count, 3);
    assert_eq!(stats.malformed_lines, 1);
    assert_eq!(ledger.latest_id().unwrap(), Some("c".repeat(32)));
}

#[test]
fn refuses_symlinks_and_public_or_linked_files() {
    let (dir, _ledger, path) = fixture();
    let ledger = Ledger::new(path.clone());
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
    assert_eq!(
        ledger.recent(1).unwrap_err().kind(),
        io::ErrorKind::PermissionDenied
    );
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let hard = dir.path().join("hard");
    fs::hard_link(&path, &hard).unwrap();
    assert!(ledger.recent(1).is_err());
    fs::remove_file(&hard).unwrap();
    let real = dir.path().join("real");
    fs::rename(&path, &real).unwrap();
    symlink(&real, &path).unwrap();
    assert!(ledger.recent(1).is_err());
}

#[test]
fn complete_appends_are_seen_and_replacement_resets_history() {
    let (_dir, ledger, path) = fixture();
    assert_eq!(ledger.query_stats().unwrap().count, 3);
    let fourth = include_bytes!("fixtures/python_ledger.jsonl")
        .split(|b| *b == b'\n')
        .next()
        .unwrap();
    let mut file = fs::OpenOptions::new().append(true).open(&path).unwrap();
    use std::io::Write;
    file.write_all(fourth).unwrap();
    assert_eq!(ledger.query_stats().unwrap().count, 3);
    file.write_all(b"\n").unwrap();
    assert_eq!(ledger.query_stats().unwrap().count, 4);
    assert_eq!(
        ledger.since(Some(&"c".repeat(32)), None, 5).unwrap().len(),
        1
    );
    drop(file);
    fs::rename(&path, path.with_extension("archived")).unwrap();
    fs::write(&path, b"").unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    assert_eq!(ledger.query_stats().unwrap().count, 0);
    assert_eq!(ledger.latest_id().unwrap(), None);
    assert!(ledger.since(Some(&"c".repeat(32)), None, 5).is_err());
}

#[test]
fn private_parent_is_required_and_missing_file_is_empty() {
    let (_dir, ledger, path) = fixture();
    let parent = path.parent().unwrap();
    fs::set_permissions(parent, fs::Permissions::from_mode(0o755)).unwrap();
    assert_eq!(
        ledger.recent(1).unwrap_err().kind(),
        io::ErrorKind::PermissionDenied
    );
    fs::set_permissions(parent, fs::Permissions::from_mode(0o700)).unwrap();
    fs::remove_file(path).unwrap();
    assert!(ledger.recent(1).unwrap().is_empty());
}

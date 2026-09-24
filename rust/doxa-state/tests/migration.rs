use doxa_state::{
    ensure_machine_id, legacy_tabset_path, load_tabset, machine_id, resolve_tabset_path,
    save_tabset, tabset_path,
};
use std::fs;
use std::io;
use std::os::unix::fs::{symlink, PermissionsExt};
use std::sync::Arc;

#[test]
fn mint_once_even_with_concurrent_callers() {
    let dir = tempfile::tempdir().unwrap();
    let home = Arc::new(dir.path().join(".doxa"));
    let threads: Vec<_> = (0..8)
        .map(|_| {
            let home = home.clone();
            std::thread::spawn(move || ensure_machine_id(&home).unwrap())
        })
        .collect();
    let ids: Vec<_> = threads
        .into_iter()
        .map(|thread| thread.join().unwrap())
        .collect();
    assert!(ids.iter().all(|id| id == &ids[0]));
    assert_eq!(ids[0].len(), 32);
    assert_eq!(ids[0].as_bytes()[12], b'4');
    assert_eq!(machine_id(&home).unwrap(), ids[0]);
    assert_eq!(
        fs::metadata(&*home).unwrap().permissions().mode() & 0o777,
        0o700
    );
    assert_eq!(
        fs::metadata(home.join("machine-id"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
}

#[test]
fn python_legacy_tabset_is_adopted_and_unknown_fields_survive() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join(".doxa");
    fs::create_dir(&home).unwrap();
    fs::set_permissions(&home, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(home.join("machine-id"), "abc").unwrap();
    fs::create_dir(home.join("tabsets")).unwrap();
    fs::set_permissions(home.join("tabsets"), fs::Permissions::from_mode(0o700)).unwrap();
    let legacy = legacy_tabset_path(&home, "/repo");
    fs::write(
        &legacy,
        include_bytes!("fixtures/python_legacy_tabset.json"),
    )
    .unwrap();
    let target = resolve_tabset_path(&home, "/repo").unwrap();
    assert_eq!(target, tabset_path(&home, "/repo", "abc"));
    assert!(!legacy.exists());
    let record = load_tabset(&target, "/repo").unwrap();
    assert_eq!(record.tabs.len(), 2);
    assert_eq!(record.raw["future_python_field"]["keep"], true);
    save_tabset(&target, &record).unwrap();
    let saved: serde_json::Value = serde_json::from_slice(&fs::read(target).unwrap()).unwrap();
    assert_eq!(saved["collections"][0]["sessions"][1], "s-two");
    assert_eq!(saved["layout"]["trees"][0]["session_id"], "s-two");
}

#[test]
fn existing_machine_tabset_wins_without_touching_legacy() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("state");
    fs::create_dir(&home).unwrap();
    fs::set_permissions(&home, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(home.join("machine-id"), "fixed").unwrap();
    fs::create_dir(home.join("tabsets")).unwrap();
    fs::set_permissions(home.join("tabsets"), fs::Permissions::from_mode(0o700)).unwrap();
    let legacy = legacy_tabset_path(&home, "/repo");
    let target = tabset_path(&home, "/repo", "fixed");
    fs::write(
        &legacy,
        include_bytes!("fixtures/python_legacy_tabset.json"),
    )
    .unwrap();
    fs::write(&target, b"owner-specific").unwrap();
    assert_eq!(resolve_tabset_path(&home, "/repo").unwrap(), target);
    assert!(legacy.exists());
    assert_eq!(fs::read(target).unwrap(), b"owner-specific");
}

#[test]
fn unsafe_legacy_and_state_directories_are_refused() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("state");
    fs::create_dir(&home).unwrap();
    fs::set_permissions(&home, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(home.join("machine-id"), "fixed").unwrap();
    fs::create_dir(home.join("tabsets")).unwrap();
    fs::set_permissions(home.join("tabsets"), fs::Permissions::from_mode(0o700)).unwrap();
    let legacy = legacy_tabset_path(&home, "/repo");
    let source = dir.path().join("source");
    fs::write(
        &source,
        include_bytes!("fixtures/python_legacy_tabset.json"),
    )
    .unwrap();
    symlink(&source, &legacy).unwrap();
    assert_eq!(
        resolve_tabset_path(&home, "/repo").unwrap_err().kind(),
        io::ErrorKind::InvalidData
    );
    assert!(!tabset_path(&home, "/repo", "fixed").exists());
    fs::remove_file(&legacy).unwrap();
    fs::set_permissions(home.join("tabsets"), fs::Permissions::from_mode(0o777)).unwrap();
    assert!(resolve_tabset_path(&home, "/repo").is_err());
    fs::set_permissions(home.join("tabsets"), fs::Permissions::from_mode(0o700)).unwrap();
    let alias = dir.path().join("alias");
    symlink(&home, &alias).unwrap();
    assert!(ensure_machine_id(&alias).is_err());
}

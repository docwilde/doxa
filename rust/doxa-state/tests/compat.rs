use doxa_state::*;
use serde_json::json;
use std::{fs, os::unix::fs::PermissionsExt};
use std::os::unix::ffi::OsStrExt;

#[test]
fn python_session_id_examples_and_path_attacks() {
    for id in ["a", "abc-123", &"A".repeat(128)] { assert!(valid_session_id(id)); }
    for id in ["", "-x", "..", "a/b", "a\\b", "a*b", "a_b", "é", &"A".repeat(129)] {
        assert!(!valid_session_id(id), "{id}");
    }
}

#[test]
fn tabset_filename_matches_python_sha256_scheme() {
    assert_eq!(
        tabset_path(std::path::Path::new("/state"), "/repo", "abc"),
        std::path::PathBuf::from("/state/tabsets/816fc349d3faebf805d1bed7-ba7816bf8f01.json")
    );
}

#[test]
fn legacy_flat_record_and_future_layout_survive_save() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("tabsets/test.json");
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::set_permissions(path.parent().unwrap(), fs::Permissions::from_mode(0o750)).unwrap();
    fs::write(&path, json!({
        "scope_key":"/repo", "active_session_id":"s2",
        "tabs":[{"session_id":"../bad"},{"session_id":"s1","pinned_name":"one","cwd":"/repo"},{"session_id":"s2"}],
        "layout":{"kind":"tabs","trees":[{"kind":"future"}]},
        "collections":[{"name":"work","sessions":["s1"]}], "future_key":{"x":1}
    }).to_string()).unwrap();
    let record = load_tabset(&path, "/repo").unwrap();
    assert_eq!(record.tabs.len(), 2);
    assert_eq!(record.active_session_id.as_deref(), Some("s2"));
    save_tabset(&path, &record).unwrap();
    let saved: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    assert_eq!(saved["layout"]["trees"][0]["kind"], "future");
    assert_eq!(saved["collections"][0]["sessions"][0], "s1");
    assert_eq!(saved["future_key"]["x"], 1);
    assert_eq!(saved["tabs"].as_array().unwrap().len(), 2);
    assert_eq!(fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
    assert_eq!(fs::metadata(path.parent().unwrap()).unwrap().permissions().mode() & 0o777, 0o750);
}

#[test]
fn config_precedence_and_parse_failure() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("config.toml");
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o750)).unwrap();
    fs::write(&path, "flag = true\nmodel = 'sonnet'\n[projects]\n'/repo' = { colour = 'blue' }\n").unwrap();
    let cfg = load_config(&path);
    assert_eq!(raw_setting(None, &cfg, "flag"), "1");
    assert_eq!(raw_setting(Some("opus"), &cfg, "model"), "opus");
    save_config(&path, &cfg).unwrap();
    assert_eq!(load_config(&path)["projects"]["/repo"]["colour"].as_str(), Some("blue"));
    fs::write(&path, "broken = [").unwrap();
    assert!(load_config(&path).is_empty());
}

#[test]
fn registry_filters_dead_and_malformed_entries() {
    let dir = tempfile::tempdir().unwrap();
    let now = time::OffsetDateTime::now_utc();
    let stamp = now.format(&time::macros::format_description!("[year]-[month]-[day]T[hour]:[minute]:[second].[subsecond digits:6]Z")).unwrap();
    let entry = json!({"session_id":"s1","pid":std::process::id(),"socket_path":"/tmp/sock","cwd":"/repo","repo_root":"/repo","title":"ok","started_at":stamp,"heartbeat_at":stamp,"daemon_socket":"/tmp/daemon"});
    fs::write(dir.path().join("ok.json"), entry.to_string()).unwrap();
    fs::write(dir.path().join("bad.json"), "{").unwrap();
    assert_eq!(list_daemons(dir.path(), Some("/repo"), str::to_owned).len(), 1);
    assert!(list_daemons(dir.path(), Some("/other"), str::to_owned).is_empty());
}

#[test]
fn registry_rejects_symlinks_oversize_and_bad_ids_and_scrubs_display_text() {
    let dir = tempfile::tempdir().unwrap();
    let now = time::OffsetDateTime::now_utc();
    let stamp = now.format(&time::macros::format_description!("[year]-[month]-[day]T[hour]:[minute]:[second].[subsecond digits:6]Z")).unwrap();
    let mut entry = json!({"session_id":"safe","pid":std::process::id(),"socket_path":"/tmp/sock","cwd":"/repo/SECRET","repo_root":"/repo","title":"SECRET","provider":"SECRET","started_at":stamp,"heartbeat_at":stamp,"daemon_socket":"/tmp/daemon","future":{"label":"SECRET"}});
    fs::write(dir.path().join("safe.json"), entry.to_string()).unwrap();
    entry["session_id"] = json!("../bad");
    fs::write(dir.path().join("bad-id.json"), entry.to_string()).unwrap();
    fs::write(dir.path().join("huge.json"), vec![b'a'; MAX_REGISTRY_BYTES as usize + 1]).unwrap();
    std::os::unix::fs::symlink(dir.path().join("safe.json"), dir.path().join("link.json")).unwrap();
    let found = list_daemons(dir.path(), Some("/repo"), |s| s.replace("SECRET", "[redacted]"));
    assert_eq!(found.len(), 1);
    assert_eq!(found[0].display.title, "[redacted]");
    assert_eq!(found[0].display.cwd, "/repo/[redacted]");
    assert_eq!(found[0].display.provider.as_deref(), Some("[redacted]"));
    assert_eq!(found[0].route.scope_key, "/repo");
    assert_eq!(found[0].route.session_id, "safe");
}

#[test]
fn tabset_refuses_membership_change_with_unparsed_layout() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("record.json");
    let original = json!({"scope_key":"/repo","tabs":[{"session_id":"s1"}],"layout":{"kind":"tabs","groups":{"kind":"future"}},"collections":[{"name":"work","sessions":["s1"]}]});
    fs::write(&path, original.to_string()).unwrap();
    let mut record = load_tabset(&path, "/repo").unwrap();
    record.tabs.clear();
    record.tabs.push(Tab { session_id: "s2".into(), pinned_name: None, cwd: None });
    assert!(save_tabset(&path, &record).is_err());
    assert_eq!(serde_json::from_slice::<serde_json::Value>(&fs::read(path).unwrap()).unwrap(), original);
}

#[test]
fn layout_only_tabset_preserves_membership_with_collections() {
    let dir = tempfile::tempdir().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o750)).unwrap();
    let path = dir.path().join("record.json");
    let original = json!({"scope_key":"/repo","layout":{"kind":"tabs","tabs":[{"session_id":"s1"}],"groups":{"kind":"future"}},"collections":[{"sessions":["s1"]}]});
    fs::write(&path, original.to_string()).unwrap();
    let mut record = load_tabset(&path, "/repo").unwrap();
    save_tabset(&path, &record).unwrap();
    let saved: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    assert_eq!(saved["tabs"][0]["session_id"], "s1");
    assert_eq!(saved["layout"]["groups"], original["layout"]["groups"]);
    record.tabs[0].session_id = "s2".into();
    assert!(save_tabset(&path, &record).is_err());
}

#[test]
fn existing_state_directory_keeps_its_mode() {
    let dir = tempfile::tempdir().unwrap();
    let parent = dir.path().join("existing");
    fs::create_dir(&parent).unwrap();
    fs::set_permissions(&parent, fs::Permissions::from_mode(0o750)).unwrap();
    save_config(&parent.join("config.toml"), &Default::default()).unwrap();
    assert_eq!(fs::metadata(&parent).unwrap().permissions().mode() & 0o777, 0o750);
    fs::set_permissions(&parent, fs::Permissions::from_mode(0o770)).unwrap();
    assert!(save_config(&parent.join("config.toml"), &Default::default()).is_err());
    assert_eq!(fs::metadata(&parent).unwrap().permissions().mode() & 0o777, 0o770);
}

#[test]
fn writes_reject_bad_ids_and_symlinked_state_directory() {
    let dir = tempfile::tempdir().unwrap();
    let real = dir.path().join("real");
    fs::create_dir(&real).unwrap();
    let alias = dir.path().join("alias");
    std::os::unix::fs::symlink(&real, &alias).unwrap();
    let mut record = TabSet {
        scope_key: "/repo".into(), active_session_id: None,
        tabs: vec![Tab { session_id: "../escape".into(), pinned_name: None, cwd: None }],
        raw: Default::default(),
    };
    assert!(save_tabset(&real.join("tab.json"), &record).is_err());
    assert!(!real.join("tab.json").exists());
    record.tabs[0].session_id = "safe-id".into();
    assert!(save_tabset(&alias.join("tab.json"), &record).is_err());
    assert!(!real.join("tab.json").exists());
}

#[test]
fn state_readers_reject_oversize_symlink_and_fifo_inputs() {
    let dir = tempfile::tempdir().unwrap();
    let tabset = dir.path().join("tabset.json");
    let config = dir.path().join("config.toml");
    let machine = dir.path().join("machine-id");
    fs::File::create(&tabset).unwrap().set_len(MAX_TABSET_BYTES + 1).unwrap();
    fs::File::create(&config).unwrap().set_len(MAX_CONFIG_BYTES + 1).unwrap();
    fs::File::create(&machine).unwrap().set_len(MAX_MACHINE_ID_BYTES + 1).unwrap();
    assert!(load_tabset(&tabset, "/repo").is_none());
    assert!(load_config(&config).is_empty());
    assert!(machine_id(dir.path()).is_err());

    fs::remove_file(&tabset).unwrap();
    fs::remove_file(&config).unwrap();
    fs::remove_file(&machine).unwrap();
    let source = dir.path().join("source");
    fs::write(&source, "valid").unwrap();
    for path in [&tabset, &config, &machine] {
        std::os::unix::fs::symlink(&source, path).unwrap();
    }
    assert!(load_tabset(&tabset, "/repo").is_none());
    assert!(load_config(&config).is_empty());
    assert!(machine_id(dir.path()).is_err());

    fs::remove_file(&tabset).unwrap();
    let fifo = std::ffi::CString::new(tabset.as_os_str().as_bytes()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(fifo.as_ptr(), 0o600) }, 0);
    assert!(load_tabset(&tabset, "/repo").is_none());
}

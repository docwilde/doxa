//! In-process canonical backend fixtures, entirely under owned roots.
use doxa_lore::LoreClient;
use std::time::Duration;

#[test]
fn native_scrub_is_lazy_and_never_creates_memory_store() {
    let owned = tempfile::tempdir().unwrap();
    let root = owned.path().join("memory-off-store");
    let mut client = LoreClient::open_config(lore_core::config::Config::for_root(root.clone()),
        Duration::from_secs(1)).unwrap();
    assert!(client.can_resolve_reviewed());
    assert_eq!(client.scrub("ordinary fixture text").unwrap(), "ordinary fixture text");
    assert!(!root.exists());
}

#[test]
fn native_backend_retains_protocol_request_bounds_without_python() {
    let owned = tempfile::tempdir().unwrap();
    let root = owned.path().join("store");
    let mut client = LoreClient::open_config(lore_core::config::Config::for_root(root.clone()),
        Duration::from_secs(1)).unwrap();
    assert!(client.scrub(&"x".repeat(doxa_lore::MAX_FRAME_BYTES)).is_err());
    assert!(!root.exists());
}


#[test]
fn native_transcript_identity_uses_canonical_projects_key_without_store_io() {
    let owned = tempfile::tempdir().unwrap();
    let root = owned.path().join("store");
    let config = lore_core::config::Config::for_root(root.clone());
    let projects = config.projects.clone();
    let mut client = LoreClient::open_config(config, Duration::from_secs(1)).unwrap();
    let (returned, slug) = client.transcript_identity(owned.path().to_str().unwrap()).unwrap();
    assert_eq!(returned, projects);
    assert!(!slug.is_empty());
    assert!(!root.exists());
}

#[test]
fn native_index_transcript_preserves_existing_indexed_and_consumed_contract() {
    use std::{fs, os::unix::fs::PermissionsExt};
    let owned = tempfile::tempdir().unwrap();
    let root = owned.path().join("store");
    let config = lore_core::config::Config::for_root(root.clone());
    let slug = lore_core::config::project_slug(owned.path());
    let directory = config.projects.join(slug);
    fs::create_dir_all(&directory).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let path = directory.join("native-owned-1.jsonl");
    let record = serde_json::json!({"type":"user", "sessionId":"native-owned-1", "cwd":owned.path(),
        "timestamp":"2026-09-27T12:00:00Z", "message":{"role":"user","content":"owned native index fixture"}});
    fs::write(&path, format!("{record}\n")).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let mut client = LoreClient::open_config(config, Duration::from_secs(1)).unwrap();
    assert_eq!(client.index_transcript(owned.path().to_str().unwrap(), "native-owned-1").unwrap(), 1);
}

#[test]
fn native_zero_capacity_preserves_usage_review_and_exact_removal() {
    use std::{fs, os::unix::fs::PermissionsExt};
    use serde_json::json;
    let owned = tempfile::tempdir().unwrap();
    let root = owned.path().join("store");
    fs::create_dir(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let path = root.join("USER.md");
    fs::write(&path, "- owned existing fact\n").unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let mut config = lore_core::config::Config::for_root(root.clone());
    config.project_cap = 0;
    config.user_cap = 0;
    config.sync.enabled = false;
    let mut client = LoreClient::open_config(config, Duration::from_secs(1)).unwrap();
    let cwd = owned.path().to_str().unwrap();
    let usage = client.memory_usage(cwd).unwrap();
    assert_eq!(usage.user_cap_chars, 0);
    assert_eq!(usage.project_cap_chars, 0);
    assert_eq!(usage.user_chars, 22);
    let reviewed = client.memory_review(cwd, "user").unwrap();
    assert_eq!(reviewed["cap_chars"], 0);
    assert_eq!(reviewed["entries"], json!(["owned existing fact"]));
    let expected = json!({"key":reviewed["key"],"sha256":reviewed["sha256"]});
    let blocked = client.memory_action(cwd, json!({"scope":"user","action":"add",
        "entry":"","text":"nonempty new fact","expected":expected}));
    assert!(matches!(blocked, Err(doxa_lore::LoreError::Remote("memory_over_cap"))));
    assert_eq!(fs::read_to_string(&path).unwrap(), "- owned existing fact\n");
    let removed = client.memory_action(cwd, json!({"scope":"user","action":"remove",
        "entry":"owned existing fact","text":"","expected":expected})).unwrap();
    assert_eq!(removed["status"], "applied");
    assert_eq!(fs::read_to_string(&path).unwrap(), "");
    assert_eq!(client.memory_usage(cwd).unwrap().user_chars, 0);
    assert_eq!(client.memory_review(cwd,"user").unwrap()["cap_chars"], 0);
    assert!(!root.join("state.db").exists());
}

#[test]
fn canonical_memory_order_preserves_exact_review_and_stale_snapshot_refusal() {
    use serde_json::json;
    use std::{fs, os::unix::fs::PermissionsExt};
    let owned = tempfile::tempdir().unwrap();
    let root = owned.path().join("store");
    fs::create_dir(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let path = root.join("USER.md");
    fs::write(&path, "- zebra fixture\n- alpha fixture\n").unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let mut config = lore_core::config::Config::for_root(root);
    config.sync.enabled = false;
    let mut client = LoreClient::open_config(config, Duration::from_secs(1)).unwrap();
    let cwd = owned.path().to_str().unwrap();
    // The bridge validates the digest against the entries' returned order.
    let reviewed = client.memory_review(cwd, "user").unwrap();
    let expected = json!({"key":reviewed["key"], "sha256":reviewed["sha256"]});
    let add = json!({"scope":"user", "action":"add", "entry":"",
        "text":"middle fixture", "expected":expected});
    assert_eq!(client.memory_action(cwd, add).unwrap()["status"], "applied");
    let canonical = "- alpha fixture\n- middle fixture\n- zebra fixture\n";
    assert_eq!(fs::read_to_string(&path).unwrap(), canonical);
    let current = client.memory_review(cwd, "user").unwrap();
    assert_eq!(
        current["entries"],
        json!(["alpha fixture", "middle fixture", "zebra fixture"])
    );
    let stale = client.memory_action(
        cwd,
        json!({"scope":"user", "action":"remove",
        "entry":"zebra fixture", "text":"", "expected":expected}),
    );
    assert!(matches!(
        stale,
        Err(doxa_lore::LoreError::Remote("memory_changed"))
    ));
    assert_eq!(fs::read_to_string(&path).unwrap(), canonical);
}

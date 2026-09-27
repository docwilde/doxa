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

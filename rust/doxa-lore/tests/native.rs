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

//! Persisted policy must stop the supervisor before a worker can return success.
use std::os::unix::fs::PermissionsExt;
use std::{
    fs,
    process::{Command, Stdio},
};

#[test]
fn saved_disabled_review_never_spawns_worker_or_emits_approval() {
    let root = tempfile::tempdir().unwrap();
    let claude = root.path().join(".claude");
    fs::create_dir(&claude).unwrap();
    let settings = claude.join("settings.json");
    let marker = root.path().join("worker-ran");
    let worker = root.path().join("worker");
    fs::write(
        &worker,
        "#!/bin/sh\nprintf ran > \"$POLICY_WORKER_MARKER\"\nexit 0\n",
    )
    .unwrap();
    fs::set_permissions(&worker, fs::Permissions::from_mode(0o700)).unwrap();
    for name in ["LORE_DISABLE_REVIEW", "LORE_SKIP"] {
        let mut values = serde_json::Map::new();
        values.insert(name.into(), serde_json::json!("1"));
        fs::write(&settings, serde_json::json!({"env":values}).to_string()).unwrap();
        fs::set_permissions(&settings, fs::Permissions::from_mode(0o600)).unwrap();
        let output = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
            .args(["__review-supervisor", "codex", "{}", "1000"])
            .env_clear()
            .env("HOME", root.path())
            .env("PATH", "/usr/bin:/bin")
            .env("DOXA_HOME", root.path().join("doxa"))
            .env("DOXA_LORE_RS", &worker)
            .env("POLICY_WORKER_MARKER", &marker)
            .stdin(Stdio::null())
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(
            output.stdout.is_empty(),
            "disabled policy emitted an approval receipt"
        );
        assert!(!marker.exists(), "disabled policy spawned a review worker");
    }
}

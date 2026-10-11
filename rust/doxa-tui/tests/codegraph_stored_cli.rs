#![cfg(unix)]
use std::{fs, process::Command};

#[test]
fn explicit_stored_cli_reports_missing_without_writing_and_rejects_symbol() {
    let owned = tempfile::tempdir().unwrap();
    let worktree = owned.path().join("worktree");
    fs::create_dir(&worktree).unwrap();
    assert!(Command::new("git").args(["init", "-q"])
        .arg(&worktree).status().unwrap().success());
    fs::write(worktree.join("lib.rs"), "mod child;\n").unwrap();
    let store = owned.path().join("lore-store");
    let run = |kind: &str| Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .env_clear().env("PATH", "/usr/bin:/bin").env("HOME", owned.path())
        .env("LORE_ROOT", &store)
        .args(["codegraph", "--stored", "--root"])
        .arg(&worktree).args([kind, "lib.rs"]).output().unwrap();
    let missing = run("modules");
    assert!(missing.status.success(), "{}", String::from_utf8_lossy(&missing.stderr));
    assert_eq!(serde_json::from_slice::<serde_json::Value>(&missing.stdout).unwrap(),
        serde_json::json!({"status":"missing"}));
    assert!(!store.exists(), "read created LORE state");
    let invalid = run("symbol");
    assert!(!invalid.status.success());
    assert!(!store.exists());
}

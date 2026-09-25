use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;

#[test]
fn native_settings_cli_persists_safely_and_explains_overrides() {
    let dir = tempfile::tempdir().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let binary = env!("CARGO_BIN_EXE_doxa-rs");
    let run = |args: &[&str], override_worktree: Option<&str>| {
        let mut command = Command::new(binary);
        command.args(args).env("DOXA_HOME", dir.path())
            .env_remove("DOXA_LINGER_SECS").env_remove("DOXA_WORKTREE");
        if let Some(value) = override_worktree { command.env("DOXA_WORKTREE", value); }
        command.output().unwrap()
    };
    let initial = run(&["settings"], None);
    assert!(initial.status.success());
    assert!(String::from_utf8_lossy(&initial.stdout).contains("linger_secs: 120 (default)"));
    assert!(run(&["settings", "set", "linger_secs", "45"], None).status.success());
    assert!(run(&["settings", "set", "worktree_per_session", "off"], None).status.success());
    let path = dir.path().join("config.toml");
    let stored: toml::Value = fs::read_to_string(&path).unwrap().parse().unwrap();
    assert_eq!(stored["linger_secs"].as_float(), Some(45.0));
    assert_eq!(stored["worktree_per_session"].as_bool(), Some(false));
    let blocked = run(&["settings", "set", "worktree_per_session", "on"], Some("0"));
    assert!(!blocked.status.success());
    assert!(String::from_utf8_lossy(&blocked.stderr).contains("DOXA_WORKTREE overrides"));
    assert_eq!(fs::read_to_string(&path).unwrap().parse::<toml::Value>().unwrap(), stored);
    assert!(run(&["settings", "unset", "linger_secs"], None).status.success());
    let after: toml::Value = fs::read_to_string(&path).unwrap().parse().unwrap();
    assert!(after.get("linger_secs").is_none());
    assert_eq!(after["worktree_per_session"].as_bool(), Some(false));
}

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;

#[test]
fn branch_option_requires_new_and_refuses_disabled_or_missing_base() {
    let binary = env!("CARGO_BIN_EXE_doxa-rs");
    let dir = tempfile::tempdir().unwrap();
    let git = |args: &[&str]| {
        let output = Command::new("git").args(args).current_dir(dir.path()).output().unwrap();
        assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
    };
    git(&["init", "-q", "-b", "main"]);
    fs::write(dir.path().join("README"), "base\n").unwrap();
    git(&["add", "README"]);
    git(&["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: base"]);
    for (args, setting, expected) in [
        (vec!["--branch", "main"], "1", "--branch requires new"),
        (vec!["new", "--engine", "fixture", "--branch", "main"], "0", "worktree_per_session"),
        (vec!["new", "--engine", "fixture", "--branch", "missing"], "1", "no such local"),
    ] {
        let result = Command::new(binary).args(args).current_dir(dir.path())
            .env("DOXA_WORKTREE", setting).output().unwrap();
        assert!(!result.status.success());
        assert!(String::from_utf8_lossy(&result.stderr).contains(expected),
            "{}", String::from_utf8_lossy(&result.stderr));
    }
}

#[test]
fn daemon_worktree_failure_reaches_launcher_error() {
    let dir = tempfile::tempdir().unwrap();
    let daemon = dir.path().join("fake-daemon");
    fs::write(&daemon, "#!/bin/sh\necho 'doxa-daemon: managed worktree unavailable; inspect conflicting doxa/ branches' >&2\nexit 1\n").unwrap();
    fs::set_permissions(&daemon, fs::Permissions::from_mode(0o700)).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args(["new", "--engine", "fixture"])
        .current_dir(dir.path())
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .output().unwrap();
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("managed worktree unavailable"), "{stderr}");
    assert!(stderr.contains("inspect conflicting doxa/ branches"), "{stderr}");
}

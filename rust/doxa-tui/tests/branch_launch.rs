use std::fs;
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

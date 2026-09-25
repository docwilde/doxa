use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;

#[test]
fn codex_resume_passes_exact_session_and_explicit_resume_flag() {
    let dir = tempfile::tempdir().unwrap();
    let daemon = dir.path().join("fake-daemon");
    let codex = dir.path().join("fake-codex");
    let capture = dir.path().join("argv.txt");
    fs::write(&daemon, "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DOXA_CAPTURE_ARGS\"\nexit 1\n").unwrap();
    fs::write(&codex, "#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(&daemon, fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(&codex, fs::Permissions::from_mode(0o700)).unwrap();
    let home = dir.path().join("home");
    fs::create_dir(&home).unwrap();
    fs::write(home.join("config.toml"), "[models]\ncodex = 'configured-model'\n").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args(["new", "--engine", "codex", "--resume", "saved-1", "--codex-bin", codex.to_str().unwrap(),
            "--lore-python", "/usr/bin/python3"])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_CAPTURE_ARGS", &capture)
        .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .env("DOXA_HOME", &home)
        .env("DOXA_MODEL", "environment-model")
        .current_dir(dir.path())
        .output().unwrap();
    assert!(!output.status.success());
    let args: Vec<_> = fs::read_to_string(capture).unwrap().lines().map(str::to_owned).collect();
    assert!(args.windows(2).any(|w| w == ["--session-id", "saved-1"]));
    assert!(args.windows(2).any(|w| w == ["--engine", "codex"]));
    assert!(args.windows(2).any(|w| w == ["--resume", "true"]));
    assert!(args.windows(2).any(|w| w == ["--codex-bin", codex.to_str().unwrap()]));
    assert!(!args.iter().any(|arg| arg == "--model"), "saved null model must stay unset");
}

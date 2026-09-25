use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;

fn fixture() -> (
    tempfile::TempDir,
    std::path::PathBuf,
    std::path::PathBuf,
    std::path::PathBuf,
) {
    let dir = tempfile::tempdir().unwrap();
    let daemon = dir.path().join("fake-daemon");
    let capture = dir.path().join("argv.txt");
    let sidecar = dir.path().join("claude_sidecar.py");
    fs::write(
        &daemon,
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DOXA_CAPTURE_ARGS\"\nexit 1\n",
    )
    .unwrap();
    fs::set_permissions(&daemon, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(&sidecar, "# fake Claude sidecar for CLI path resolution\n").unwrap();
    (dir, daemon, capture, sidecar)
}

#[test]
fn new_claude_passes_explicit_identity_and_paths_to_native_daemon() {
    let (dir, daemon, capture, sidecar) = fixture();
    let output = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args([
            "new",
            "--engine",
            "claude",
            "--claude-python",
            "/usr/bin/python3",
            "--claude-script",
            sidecar.to_str().unwrap(),
            "--resume",
            "session-1",
            "--model",
            "claude-test",
        ])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_CAPTURE_ARGS", &capture)
        .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .current_dir(dir.path())
        .output()
        .unwrap();
    assert!(
        !output.status.success(),
        "fake daemon intentionally exits before registration"
    );
    let args: Vec<_> = fs::read_to_string(capture)
        .unwrap()
        .lines()
        .map(str::to_owned)
        .collect();
    assert!(args.windows(2).any(|w| w == ["--engine", "claude"]));
    assert!(args.windows(2).any(|w| w == ["--session-id", "session-1"]));
    assert!(args.windows(2).any(|w| w == ["--resume", "true"]));
    assert!(args.windows(2).any(|w| w == ["--model", "claude-test"]));
    assert!(args
        .windows(2)
        .any(|w| w == ["--claude-script", sidecar.to_str().unwrap()]));
    assert!(!args
        .iter()
        .any(|arg| arg == "--codex-bin" || arg == "--lore-python"));
    assert!(String::from_utf8_lossy(&output.stderr).contains("Claude SDK and sidecar"));
}

#[test]
fn claude_doctor_checks_selected_dependencies_and_relative_script_is_rejected() {
    let (dir, daemon, capture, sidecar) = fixture();
    let doctor = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args([
            "doctor",
            "--engine",
            "claude",
            "--claude-python",
            "/usr/bin/python3",
            "--claude-script",
            sidecar.to_str().unwrap(),
        ])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .output()
        .unwrap();
    assert!(
        doctor.status.success(),
        "{}",
        String::from_utf8_lossy(&doctor.stderr)
    );
    let output = String::from_utf8_lossy(&doctor.stdout);
    assert!(output.contains("ok claude python:"));
    assert!(output.contains("ok claude sidecar:"));
    assert!(!output.contains("codex:"));
    let invalid = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args([
            "new",
            "--engine",
            "claude",
            "--claude-python",
            "/usr/bin/python3",
            "--claude-script",
            "relative.py",
        ])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_CAPTURE_ARGS", &capture)
        .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .current_dir(dir.path())
        .output()
        .unwrap();
    assert!(!invalid.status.success());
    assert!(!capture.exists(), "invalid launch must not start a daemon");
}

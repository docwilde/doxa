use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::process::Command;

fn executable(path: &Path, body: &str) {
    fs::write(path, body).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
}

fn live_session(runtime: &Path, cwd: &Path) -> UnixListener {
    fs::create_dir(runtime).unwrap();
    let registry = runtime.join("registry");
    fs::create_dir(&registry).unwrap();
    fs::set_permissions(runtime, fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(&registry, fs::Permissions::from_mode(0o700)).unwrap();
    let socket = runtime.join(format!("daemon-existing-{}.sock", std::process::id()));
    let listener = UnixListener::bind(&socket).unwrap();
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
    let entry = serde_json::json!({
        "session_id": "existing", "cwd": cwd, "pid": std::process::id(),
        "heartbeat_at": "2099-01-01T00:00:00Z", "started_at": "2099-01-01T00:00:00Z",
        "title": "existing", "daemon_socket": socket,
    });
    let path = registry.join("existing.json");
    fs::write(&path, entry.to_string()).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    listener
}

fn args(path: PathBuf) -> Vec<String> {
    fs::read_to_string(path).unwrap().lines().map(str::to_owned).collect()
}

#[test]
fn explicit_engine_and_model_start_new_session_with_existing_live_session() {
    let dir = tempfile::tempdir().unwrap();
    let runtime = dir.path().join("runtime");
    let _listener = live_session(&runtime, dir.path());
    let daemon = dir.path().join("fake-daemon");
    let capture = dir.path().join("argv.txt");
    executable(&daemon, "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DOXA_CAPTURE_ARGS\"\nexit 1\n");
    let output = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args(["--engine", "deepseek", "--model", "deepseek-test", "--lore-python", "/usr/bin/python3"])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_CAPTURE_ARGS", &capture)
        .env("DOXA_RUNTIME_DIR", &runtime)
        .env("DEEPSEEK_API_KEY", "test-key")
        .current_dir(dir.path())
        .output().unwrap();
    assert!(!output.status.success(), "fake daemon intentionally exits");
    let argv = args(capture);
    assert!(argv.windows(2).any(|pair| pair == ["--engine", "deepseek"]));
    assert!(argv.windows(2).any(|pair| pair == ["--model", "deepseek-test"]));
    assert!(argv.windows(2).any(|pair| pair[0] == "--session-id" && pair[1] != "existing"));
}

#[test]
fn model_alone_starts_codex_and_invalid_vendor_does_not_start_daemon() {
    let dir = tempfile::tempdir().unwrap();
    let runtime = dir.path().join("runtime");
    let _listener = live_session(&runtime, dir.path());
    let daemon = dir.path().join("fake-daemon");
    let codex = dir.path().join("fake-codex");
    let capture = dir.path().join("argv.txt");
    executable(&daemon, "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DOXA_CAPTURE_ARGS\"\nexit 1\n");
    executable(&codex, "#!/bin/sh\nexit 0\n");
    let output = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args(["--model", "codex-test", "--codex-bin", codex.to_str().unwrap()])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_CAPTURE_ARGS", &capture)
        .env("DOXA_RUNTIME_DIR", &runtime)
        .current_dir(dir.path())
        .output().unwrap();
    assert!(!output.status.success());
    let argv = args(capture.clone());
    assert!(argv.windows(2).any(|pair| pair == ["--engine", "codex"]));
    assert!(argv.windows(2).any(|pair| pair == ["--model", "codex-test"]));

    fs::remove_file(&capture).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args(["--engine", "fixture"])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_CAPTURE_ARGS", &capture)
        .env("DOXA_RUNTIME_DIR", &runtime)
        .current_dir(dir.path())
        .output().unwrap();
    assert!(!output.status.success());
    assert!(args(capture.clone()).windows(2).any(|pair| pair == ["--engine", "fixture"]));

    fs::remove_file(&capture).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args(["--engine", "deepseek", "--model", "deepseek-test"])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_CAPTURE_ARGS", &capture)
        .env("DOXA_RUNTIME_DIR", &runtime)
        .env_remove("DEEPSEEK_API_KEY")
        .current_dir(dir.path())
        .output().unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("DEEPSEEK_API_KEY is required"));
    assert!(!capture.exists());

    let output = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args(["--model", "", "--codex-bin", codex.to_str().unwrap()])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_CAPTURE_ARGS", &capture)
        .env("DOXA_RUNTIME_DIR", &runtime)
        .current_dir(dir.path())
        .output().unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("invalid model"));
    assert!(!capture.exists());
}

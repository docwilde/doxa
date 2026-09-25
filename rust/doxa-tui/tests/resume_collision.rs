use serde_json::json;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixListener;
use std::process::Command;
use std::time::{Duration, Instant};

fn assert_resume_ignores_existing_same_id_daemon(vendor: bool) {
    let dir = tempfile::tempdir().unwrap();
    let runtime = dir.path().join("runtime");
    let registry = runtime.join("registry");
    fs::create_dir_all(&registry).unwrap();
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(&registry, fs::Permissions::from_mode(0o700)).unwrap();
    let id = "session-1";
    let socket = runtime.join(format!("daemon-{}-{}.sock", &id[..8], std::process::id()));
    let listener = UnixListener::bind(&socket).unwrap();
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
    listener.set_nonblocking(true).unwrap();
    let now = time::OffsetDateTime::now_utc()
        .format(&time::format_description::well_known::Rfc3339)
        .unwrap();
    let entry = json!({"session_id":id,"pid":std::process::id(),
        "cwd":dir.path(),"repo_root":null,"title":"old session",
        "daemon_socket":socket,"clients":0,"started_at":now,"heartbeat_at":now});
    let record = registry.join(format!("{id}.json"));
    fs::write(&record, entry.to_string()).unwrap();
    fs::set_permissions(&record, fs::Permissions::from_mode(0o600)).unwrap();
    let daemon = dir.path().join("fake-daemon");
    fs::write(&daemon, "#!/bin/sh\nsleep 0.2\nexit 1\n").unwrap();
    fs::set_permissions(&daemon, fs::Permissions::from_mode(0o700)).unwrap();
    let sidecar = dir.path().join("claude_sidecar.py");
    fs::write(&sidecar, "# fixture\n").unwrap();
    let old_socket = std::thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < deadline {
            match listener.accept() {
                Ok((_stream, _)) => return true,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(10));
                }
                Err(error) => panic!("old socket accept: {error}"),
            }
        }
        false
    });
    let mut command = Command::new(env!("CARGO_BIN_EXE_doxa-rs"));
    command.args([
        "new",
        "--engine",
        if vendor { "deepseek" } else { "claude" },
    ]);
    if vendor {
        command.args(["--lore-python", "/usr/bin/python3"]);
        command.env("DEEPSEEK_API_KEY", "secret-vendor-key");
    } else {
        command.args([
            "--claude-python",
            "/usr/bin/python3",
            "--claude-script",
            sidecar.to_str().unwrap(),
        ]);
    }
    let output = command
        .args(["--resume", id])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_RUNTIME_DIR", &runtime)
        .current_dir(dir.path())
        .output()
        .unwrap();
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("native daemon exited before startup"),
        "{stderr}"
    );
    assert!(!stderr.contains("started native session"));
    assert!(
        !old_socket.join().unwrap(),
        "launch attached to preexisting same-ID daemon"
    );
}

#[test]
fn resume_ignores_existing_same_id_daemon_from_another_pid() {
    assert_resume_ignores_existing_same_id_daemon(false);
}

#[test]
fn vendor_resume_ignores_existing_same_id_daemon_from_another_pid() {
    assert_resume_ignores_existing_same_id_daemon(true);
}

#![cfg(unix)]
use std::{fs, os::unix::fs::{symlink, PermissionsExt}, process::Command};

#[test]
fn doctor_resolves_installed_codex_and_reports_socket_budget() {
    let root = tempfile::tempdir().unwrap();
    let runtime = root.path().join("runtime");
    fs::create_dir(&runtime).unwrap();
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
    let data = root.path().join("data");
    let providers = data.join("doxa/providers");
    let payload = providers.join("reviewed");
    fs::create_dir_all(&payload).unwrap();
    let installed = payload.join("codex");
    fs::write(&installed, "#!/bin/sh\nexit 99\n").unwrap();
    fs::set_permissions(&installed, fs::Permissions::from_mode(0o700)).unwrap();
    symlink("reviewed", providers.join("codex-current")).unwrap();
    let doctor = || {
        let mut command = Command::new(env!("CARGO_BIN_EXE_doxa-rs"));
        command.env_clear().args(["doctor", "--engine", "codex"])
            .env("HOME", root.path()).env("PATH", "/usr/bin:/bin")
            .env("XDG_DATA_HOME", &data).env("DOXA_HOME", root.path().join("state"))
            .env("DOXA_DAEMON_BIN", env!("CARGO_BIN_EXE_doxa-rs"))
            .env("DOXA_RUNTIME_DIR", &runtime);
        command
    };
    let output = doctor().output().unwrap();
    let report = String::from_utf8_lossy(&output.stdout);
    assert!(report.contains(&format!("ok codex: {}", installed.display())), "{report}");

    let oversized = root.path().join("long".repeat(40));
    let output = doctor().args(["--isolation", "docker-offline"])
        .env("DOXA_HOME", &oversized).output().unwrap();
    let report = String::from_utf8_lossy(&output.stdout);
    assert!(!output.status.success());
    assert!(report.contains("missing isolation socket paths: DOXA_HOME is"), "{report}");
    assert!(report.contains("Unix socket path limit"), "{report}");
    assert!(!oversized.join("isolation").exists());

    // A broken reviewed pointer must remain an error even when PATH has a CLI.
    fs::remove_file(&installed).unwrap();
    let output = doctor().output().unwrap();
    assert!(String::from_utf8_lossy(&output.stdout).contains("missing codex:"));
}

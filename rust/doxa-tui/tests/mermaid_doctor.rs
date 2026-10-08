use std::{fs, os::unix::fs::PermissionsExt, process::Command};

#[test]
fn doctor_reports_disabled_and_bad_renderer_without_exposing_paths() {
    let home = std::env::var("HOME").unwrap();
    let scratch = std::path::Path::new(&home).join(".cache/doxa-tests");
    fs::create_dir_all(&scratch).unwrap();
    let dir = tempfile::tempdir_in(scratch).unwrap();
    let runtime = dir.path().join("runtime");
    fs::create_dir(&runtime).unwrap();
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
    let doctor = || {
        let mut command = Command::new(env!("CARGO_BIN_EXE_doxa-rs"));
        command.args(["doctor", "--engine", "fixture"])
            .env("DOXA_DAEMON_BIN", env!("CARGO_BIN_EXE_doxa-rs"))
            .env("DOXA_RUNTIME_DIR", &runtime)
            .env("DOXA_HOME", dir.path().join("state"))
            .env_remove("DOXA_MERMAID_RENDERER")
            .env_remove("DOXA_MERMAID_RENDERER_ROOT");
        command
    };
    let disabled = doctor().output().unwrap();
    assert!(disabled.status.success(), "{}", String::from_utf8_lossy(&disabled.stderr));
    assert!(String::from_utf8_lossy(&disabled.stdout).contains("disabled mermaid:"));

    let secret = "SECRET_MERMAID_PATH_TOKEN_7531";
    let invalid = doctor()
        .env("DOXA_MERMAID_RENDERER", format!("/missing/{secret}"))
        .env("DOXA_MERMAID_RENDERER_ROOT", "/missing/package")
        .output().unwrap();
    assert!(!invalid.status.success());
    let output = format!("{}{}", String::from_utf8_lossy(&invalid.stdout),
        String::from_utf8_lossy(&invalid.stderr));
    assert!(output.contains("missing mermaid: renderer package root is unavailable"));
    assert!(!output.contains(secret));
}

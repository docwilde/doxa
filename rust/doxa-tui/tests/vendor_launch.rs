use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;

fn fixture() -> (tempfile::TempDir, std::path::PathBuf, std::path::PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let daemon = dir.path().join("fake-daemon");
    let capture = dir.path().join("argv.txt");
    fs::write(
        &daemon,
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DOXA_CAPTURE_ARGS\"\nexit 1\n",
    )
    .unwrap();
    fs::set_permissions(&daemon, fs::Permissions::from_mode(0o700)).unwrap();
    (dir, daemon, capture)
}

fn argv(path: &std::path::Path) -> Vec<String> {
    fs::read_to_string(path)
        .unwrap()
        .lines()
        .map(str::to_owned)
        .collect()
}

#[test]
fn new_deepseek_passes_only_vendor_options_and_never_key() {
    let (dir, daemon, capture) = fixture();
    let output = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args([
            "new",
            "--engine",
            "deepseek",
            "--lore-python",
            "/usr/bin/python3",
            "--model",
            "deepseek-test",
            "--effort",
            "none",
        ])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_CAPTURE_ARGS", &capture)
        .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .env("DEEPSEEK_API_KEY", "secret-vendor-key")
        .env("DOXA_MODEL", "codex-only-model")
        .current_dir(dir.path())
        .output()
        .unwrap();
    assert!(!output.status.success());
    let args = argv(&capture);
    assert!(args.windows(2).any(|w| w == ["--engine", "deepseek"]));
    assert!(args.windows(2).any(|w| w == ["--model", "deepseek-test"]));
    assert!(args.windows(2).any(|w| w == ["--effort", "none"]));
    let python = "/usr/bin/python3";
    assert!(args
        .windows(2)
        .any(|w| w[0] == "--lore-python" && w[1] == python));
    assert!(!args.join(" ").contains("secret-vendor-key"));
    assert!(!args.iter().any(|a| matches!(
        a.as_str(),
        "--codex-bin" | "--sandbox" | "--claude-python" | "--vendor-endpoint"
    )));
    assert!(!String::from_utf8_lossy(&output.stderr).contains("secret-vendor-key"));
}

#[test]
fn glm_uses_its_own_configured_model_and_doctor_checks_key_without_printing_it() {
    let (dir, daemon, capture) = fixture();
    let home = dir.path().join("home");
    fs::create_dir_all(&home).unwrap();
    fs::write(
        home.join("config.toml"),
        "[models]\ncodex = 'codex-only'\nglm = 'glm-config'\n",
    )
    .unwrap();
    let doctor = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args([
            "doctor",
            "--engine",
            "glm",
            "--lore-python",
            "/usr/bin/python3",
        ])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .env("DOXA_HOME", &home)
        .env("ZAI_API_KEY", "secret-vendor-key")
        .output()
        .unwrap();
    assert!(
        doctor.status.success(),
        "{}",
        String::from_utf8_lossy(&doctor.stderr)
    );
    let summary = String::from_utf8_lossy(&doctor.stdout);
    assert!(summary.contains("ok lore python:"));
    assert!(summary.contains("ok ZAI_API_KEY: set"));
    assert!(!summary.contains("secret-vendor-key"));
    assert!(!summary.contains("codex:"));
    let launched = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args([
            "new",
            "--engine",
            "glm",
            "--lore-python",
            "/usr/bin/python3",
        ])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_CAPTURE_ARGS", &capture)
        .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .env("DOXA_HOME", &home)
        .env("DOXA_MODEL", "codex-env-only")
        .env("ZAI_API_KEY", "secret-vendor-key")
        .current_dir(dir.path())
        .output()
        .unwrap();
    assert!(!launched.status.success());
    let args = argv(&capture);
    assert!(args.windows(2).any(|w| w == ["--engine", "glm"]));
    assert!(args.windows(2).any(|w| w == ["--model", "glm-config"]));
    assert!(!args.iter().any(|arg| arg == "--effort"));
    assert!(!args.join(" ").contains("codex"));
}

#[test]
fn invalid_vendor_effort_and_missing_key_never_start_daemon() {
    let (dir, daemon, capture) = fixture();
    for (args, key) in [
        (
            vec!["new", "--engine", "glm", "--effort", "none"],
            Some("secret"),
        ),
        (vec!["new", "--engine", "deepseek"], None),
    ] {
        let mut command = Command::new(env!("CARGO_BIN_EXE_doxa-rs"));
        command
            .args(args)
            .env("DOXA_DAEMON_BIN", &daemon)
            .env("DOXA_CAPTURE_ARGS", &capture)
            .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
            .env_remove("DEEPSEEK_API_KEY")
            .env_remove("ZAI_API_KEY")
            .current_dir(dir.path());
        if let Some(key) = key {
            command.env("ZAI_API_KEY", key);
        }
        let output = command.output().unwrap();
        assert!(!output.status.success());
        assert!(!capture.exists());
    }
}

#[test]
fn vendor_resume_passes_exact_identity_and_boolean_without_credentials() {
    for (engine, key) in [("deepseek", "DEEPSEEK_API_KEY"), ("glm", "ZAI_API_KEY")] {
        let (dir, daemon, capture) = fixture();
        let output = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
            .args([
                "new",
                "--engine",
                engine,
                "--resume",
                "vendor-session-123",
                "--lore-python",
                "/usr/bin/python3",
            ])
            .env("DOXA_DAEMON_BIN", &daemon)
            .env("DOXA_CAPTURE_ARGS", &capture)
            .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
            .env(key, "secret-vendor-key")
            .current_dir(dir.path())
            .output()
            .unwrap();
        assert!(!output.status.success());
        let args = argv(&capture);
        assert!(args.windows(2).any(|w| w == ["--engine", engine]));
        assert!(args
            .windows(2)
            .any(|w| w == ["--session-id", "vendor-session-123"]));
        assert!(args.windows(2).any(|w| w == ["--resume", "true"]));
        assert!(!args.join(" ").contains("secret-vendor-key"));
        assert!(!args
            .iter()
            .any(|arg| arg == "--codex-bin" || arg == "--claude-script"));
        assert!(!String::from_utf8_lossy(&output.stderr).contains("secret-vendor-key"));
    }
}

#[test]
fn vendor_resume_rejects_invalid_identity_and_non_new_command_before_spawn() {
    let (dir, daemon, capture) = fixture();
    for args in [
        vec!["new", "--engine", "deepseek", "--resume", "bad/../id"],
        vec!["new", "--engine", "glm", "--resume", "-bad"],
        vec![
            "doctor",
            "--engine",
            "glm",
            "--resume",
            "vendor-session-123",
        ],
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
            .args(args)
            .env("DOXA_DAEMON_BIN", &daemon)
            .env("DOXA_CAPTURE_ARGS", &capture)
            .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
            .env("DEEPSEEK_API_KEY", "secret-vendor-key")
            .env("ZAI_API_KEY", "secret-vendor-key")
            .current_dir(dir.path())
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(!capture.exists());
    }
}

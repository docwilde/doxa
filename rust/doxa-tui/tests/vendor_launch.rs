use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;

fn fixture() -> (tempfile::TempDir, std::path::PathBuf, std::path::PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("home");
    fs::create_dir(&home).unwrap();
    fs::set_permissions(&home, fs::Permissions::from_mode(0o700)).unwrap();
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
            "--model",
            "deepseek-test",
            "--effort",
            "none",
        ])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_CAPTURE_ARGS", &capture)
        .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .env("DOXA_HOME", dir.path().join("home"))
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
    assert!(!args.iter().any(|arg|arg=="--lore-python"));
    assert!(!args.join(" ").contains("secret-vendor-key"));
    assert!(!args.iter().any(|a| matches!(
        a.as_str(),
        "--codex-bin" | "--sandbox" | "--claude-bin" | "--vendor-endpoint"
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
            ])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .env("DOXA_HOME", dir.path().join("home"))
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
    assert!(summary.contains("ok daemon:"));
    assert!(!summary.contains("lore python"));
    assert!(summary.contains("ok ZAI_API_KEY: set"));
    assert!(!summary.contains("secret-vendor-key"));
    assert!(!summary.contains("codex:"));
    let launched = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args([
            "new",
            "--engine",
            "glm",
            ])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_CAPTURE_ARGS", &capture)
        .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .env("DOXA_HOME", dir.path().join("home"))
        .env("DOXA_HOME", &home)
        .env_remove("DOXA_MODEL")
        .env_remove("DOXA_EFFORT")
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
    // Like Python's model_provenance, the explicit process-wide override
    // applies to every engine; per-engine config applies when it is absent.
    let override_launch = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args(["new", "--engine", "glm", ])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("DOXA_CAPTURE_ARGS", &capture)
        .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .env("DOXA_HOME", dir.path().join("home"))
        .env("DOXA_HOME", &home)
        .env("DOXA_MODEL", "glm-env-override")
        .env_remove("DOXA_EFFORT")
        .env("ZAI_API_KEY", "secret-vendor-key")
        .current_dir(dir.path())
        .output().unwrap();
    assert!(!override_launch.status.success());
    assert!(argv(&capture).windows(2).any(|w| w == ["--model", "glm-env-override"]));
    assert!(!String::from_utf8_lossy(&override_launch.stderr).contains("secret-vendor-key"));
}

#[test]
fn invalid_vendor_effort_and_missing_key_never_start_daemon() {
    let (dir, daemon, capture) = fixture();
    for (args, key) in [
        (
            vec!["new", "--engine", "glm", "--effort", "none"],
            Some("synthetic-invalid-effort-key"),
        ),
        (vec!["new", "--engine", "deepseek"], None),
    ] {
        let mut command = Command::new(env!("CARGO_BIN_EXE_doxa-rs"));
        command
            .args(args)
            .env("DOXA_DAEMON_BIN", &daemon)
            .env("DOXA_CAPTURE_ARGS", &capture)
            .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .env("DOXA_HOME", dir.path().join("home"))
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
                ])
            .env("DOXA_DAEMON_BIN", &daemon)
            .env("DOXA_CAPTURE_ARGS", &capture)
            .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .env("DOXA_HOME", dir.path().join("home"))
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
        .env("DOXA_HOME", dir.path().join("home"))
            .env("DEEPSEEK_API_KEY", "secret-vendor-key")
            .env("ZAI_API_KEY", "secret-vendor-key")
            .current_dir(dir.path())
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(!capture.exists());
    }
}

#[test]
fn saved_only_keys_pass_launch_and_doctor_without_exposing_values_and_unsafe_store_refuses() {
    for (engine, key_name) in [("deepseek", "deepseek"), ("glm", "glm")] {
        let (dir, daemon, capture) = fixture();
        let home = dir.path().join("home");
        let store = home.join("credentials.json");
        let secret = "synthetic-saved-only-api-key";
        fs::write(&store, serde_json::json!({key_name: secret}).to_string()).unwrap();
        fs::set_permissions(&store, fs::Permissions::from_mode(0o600)).unwrap();
        let command = || {
            let mut command = Command::new(env!("CARGO_BIN_EXE_doxa-rs"));
            command.env("DOXA_HOME", &home).env("DOXA_DAEMON_BIN", &daemon)
                .env("DOXA_CAPTURE_ARGS", &capture).env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
                .env_remove("DEEPSEEK_API_KEY").env_remove("ZAI_API_KEY")
                .env_remove("DOXA_MODEL").env_remove("DOXA_EFFORT").current_dir(dir.path());
            command
        };
        let doctor = command().args(["doctor", "--engine", engine]).output().unwrap();
        assert!(doctor.status.success(), "{}", String::from_utf8_lossy(&doctor.stderr));
        assert!(String::from_utf8_lossy(&doctor.stdout).contains("set (saved)"));
        assert!(!String::from_utf8_lossy(&doctor.stdout).contains(secret));
        let launched = command().args(["new", "--engine", engine]).output().unwrap();
        assert!(!launched.status.success()); // Fake daemon records argv then exits.
        assert!(argv(&capture).windows(2).any(|w| w == ["--engine", engine]));
        assert!(!argv(&capture).join(" ").contains(secret));
        assert!(!String::from_utf8_lossy(&launched.stderr).contains(secret));
        fs::remove_file(&capture).unwrap();
        fs::set_permissions(&store, fs::Permissions::from_mode(0o644)).unwrap();
        let refused = command().args(["new", "--engine", engine]).output().unwrap();
        assert!(!refused.status.success());
        assert!(!capture.exists());
        assert!(String::from_utf8_lossy(&refused.stderr).contains("credential store is unavailable"));
        assert!(!String::from_utf8_lossy(&refused.stderr).contains(secret));
    }
}

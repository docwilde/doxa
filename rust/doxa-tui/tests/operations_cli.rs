use std::{fs, process::Command};
use std::os::unix::fs::PermissionsExt;

#[test]
fn plugins_cli_updates_only_doxa_policy_and_respects_environment() {
    let home = tempfile::tempdir().unwrap();
    fs::set_permissions(home.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let claude = tempfile::tempdir().unwrap();
    fs::write(claude.path().join("settings.json"), "{\"enabledPlugins\":{\"lore@lore\":true}}").unwrap();
    let original = fs::read(claude.path().join("settings.json")).unwrap();
    let sidecar = home.path().join("plugin-sidecar.py");
    fs::write(&sidecar, "import sys\nassert sys.flags.isolated\nassert sys.argv[1:] == ['--reload-plugins']\nprint('adoption: ON · re-staged fixture only; NEW sessions')\n").unwrap();
    let run = |args: &[&str], override_value: Option<&str>| {
        let mut command = Command::new(env!("CARGO_BIN_EXE_doxa-rs"));
        command.args(args).env("DOXA_HOME", home.path()).env("CLAUDE_CONFIG_DIR", claude.path()).env_remove("DOXA_ADOPT_PLUGINS").env("DOXA_CLAUDE_SCRIPT", &sidecar);
        if let Some(value) = override_value { command.env("DOXA_ADOPT_PLUGINS", value); }
        command.output().unwrap()
    };
    assert!(run(&["plugins", "adopt", "on"], None).status.success());
    let config: toml::Value = fs::read_to_string(home.path().join("config.toml")).unwrap().parse().unwrap();
    assert_eq!(config["adopt_plugins"].as_bool(), Some(true));
    let refresh = run(&["plugins", "refresh"], None);
    assert!(refresh.status.success());
    assert!(String::from_utf8_lossy(&refresh.stdout).contains("adoption: ON"));
    assert!(!run(&["plugins", "adopt", "off"], Some("1")).status.success());
    assert_eq!(fs::read(claude.path().join("settings.json")).unwrap(), original);
    assert!(!run(&["auth", "login"], None).status.success());
    assert!(!run(&["auth", "logout", "inferred"], None).status.success());
}

#[test]
fn plugin_refresh_bridge_rejects_failed_or_oversized_private_output() {
    let home = tempfile::tempdir().unwrap();
    let sidecar = home.path().join("plugin-sidecar.py");
    let run = || Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args(["plugins", "refresh"]).env("DOXA_HOME", home.path())
        .env("DOXA_CLAUDE_SCRIPT", &sidecar).output().unwrap();
    fs::write(&sidecar, "import sys\nprint('private credential sentinel')\nprint('private exception sentinel', file=sys.stderr)\nsys.exit(1)\n").unwrap();
    let failed = run();
    assert!(!failed.status.success());
    assert!(!String::from_utf8_lossy(&failed.stdout).contains("sentinel"));
    assert!(!String::from_utf8_lossy(&failed.stderr).contains("sentinel"));
    fs::write(&sidecar, "print('x' * 65537)\n").unwrap();
    let large = run();
    assert!(!large.status.success());
    assert!(large.stdout.len() < 1024 && large.stderr.len() < 1024);
}

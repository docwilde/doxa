use std::{fs, process::Command};
use std::os::unix::fs::PermissionsExt;

#[test]
fn plugins_cli_updates_only_doxa_policy_and_respects_environment() {
    let home = tempfile::tempdir().unwrap();
    fs::set_permissions(home.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let claude = tempfile::tempdir().unwrap();
    fs::write(claude.path().join("settings.json"), "{\"enabledPlugins\":{\"lore@lore\":true}}").unwrap();
    let original = fs::read(claude.path().join("settings.json")).unwrap();
    let run = |args: &[&str], override_value: Option<&str>| {
        let mut command = Command::new(env!("CARGO_BIN_EXE_doxa-rs"));
        command.args(args).env("DOXA_HOME", home.path()).env("CLAUDE_CONFIG_DIR", claude.path()).env_remove("DOXA_ADOPT_PLUGINS");
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

//! Read-only setup checks. Provider CLIs own their credentials; only probe
//! exit status is observed, and their output is never captured or displayed.

use std::io;
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthState {
    Missing,
    Authenticated,
    Unauthenticated,
    TimedOut,
}

struct Provider {
    name: &'static str,
    label: &'static str,
    binary: &'static str,
    probe: &'static [&'static str],
}

const PROVIDERS: &[Provider] = &[
    Provider { name: "claude", label: "Claude (Anthropic)", binary: "claude", probe: &["auth", "status"] },
    Provider { name: "codex", label: "Codex (OpenAI)", binary: "codex", probe: &["login", "status"] },
];

fn probe(binary: &Path, args: &[&str], timeout: Duration) -> AuthState {
    let Ok(mut child) = Command::new(binary).args(args)
        .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
        .process_group(0).spawn()
    else { return AuthState::Unauthenticated };
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return if status.success() { AuthState::Authenticated } else { AuthState::Unauthenticated },
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(25)),
            Ok(None) => {
                if let Ok(pid) = i32::try_from(child.id()) {
                    // Provider CLIs can spawn helpers. Reap the isolated group
                    // so a timed-out status probe leaves none running.
                    unsafe { libc::kill(-pid, libc::SIGKILL) };
                }
                let _ = child.kill();
                let _ = child.wait();
                return AuthState::TimedOut;
            }
            Err(_) => {
                if let Ok(pid) = i32::try_from(child.id()) {
                    unsafe { libc::kill(-pid, libc::SIGKILL) };
                }
                let _ = child.kill();
                let _ = child.wait();
                return AuthState::Unauthenticated;
            }
        }
    }
}

fn locate(binary: &str) -> Option<PathBuf> {
    crate::launch::executable(Path::new(binary)).ok()
}

fn provider_state(provider: &Provider) -> AuthState {
    let Some(path) = locate(provider.binary) else { return AuthState::Missing };
    probe(&path, provider.probe, Duration::from_secs(10))
}

fn state_text(state: AuthState) -> &'static str {
    match state {
        AuthState::Missing => "CLI not installed",
        AuthState::Authenticated => "authenticated",
        AuthState::Unauthenticated => "not authenticated",
        AuthState::TimedOut => "auth status timed out",
    }
}

pub fn auth_status(name: Option<&str>) -> io::Result<String> {
    let selected: Vec<&Provider> = if let Some(name) = name {
        vec![PROVIDERS.iter().find(|provider| provider.name == name)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput,
                "usage: doxa auth status [claude|codex]"))?]
    } else { PROVIDERS.iter().collect() };
    Ok(selected.into_iter().map(|provider| format!("{}: {}", provider.label,
        state_text(provider_state(provider)))).collect::<Vec<_>>().join("\n"))
}

fn doxa_home() -> io::Result<PathBuf> {
    std::env::var_os("DOXA_HOME").filter(|value| !value.is_empty()).map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|value| PathBuf::from(value).join(".doxa")))
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "DOXA_HOME and HOME are unset"))
}

fn safe_report_value(value: &str) -> String {
    value.chars().filter(|c| !c.is_control()
        && !matches!(*c, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}'))
        .take(200).collect()
}

fn preference(config: &toml::Table, key: &str, env: &str) -> String {
    if let Some(value) = std::env::var(env).ok().filter(|value| !value.trim().is_empty()) {
        return format!("{} (environment)", safe_report_value(&value));
    }
    if let Some(value) = config.get(key).and_then(toml::Value::as_str).filter(|value| !value.is_empty()) {
        return format!("{} (config.toml)", safe_report_value(value));
    }
    "(CLI default)".to_owned()
}

pub fn setup_report() -> io::Result<String> {
    let home = doxa_home()?;
    let config = doxa_state::load_config(&home.join("config.toml"));
    let lore = if let Some(root) = std::env::var("LORE_ROOT").ok().filter(|root| !root.trim().is_empty()) {
        format!("{} (environment)", safe_report_value(&root))
    } else if let Some(root) = config.get("lore_root").and_then(toml::Value::as_str).filter(|root| !root.is_empty()) {
        format!("{} (config.toml)", safe_report_value(root))
    } else {
        let plugin = std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".claude/lore"));
        if let Some(path) = plugin.filter(|path| path.is_dir()) {
            format!("unselected; existing Claude LORE store at {}", safe_report_value(&path.display().to_string()))
        } else {
            format!("unselected; suggested DOXA store at {}", safe_report_value(&home.join("lore").display().to_string()))
        }
    };
    Ok(format!(
        "auth state\n{}\n\nSign in with the provider CLI: claude auth login or codex login. DOXA never asks for credentials.\n\nLORE store\n{lore}\n\nmodel & effort defaults (stored preferences)\nmodel: {}\neffort: {}\n\nUse `doxa settings` for native linger and worktree preferences; the Python UI manages its wider settings catalog.",
        auth_status(None)?, preference(&config, "model", "DOXA_MODEL"),
        preference(&config, "effort", "DOXA_EFFORT"),
    ))
}

fn setting_env(key: &str) -> Option<&'static str> {
    match key {
        "linger_secs" => Some("DOXA_LINGER_SECS"),
        "worktree_per_session" => Some("DOXA_WORKTREE"),
        _ => None,
    }
}

fn effective_setting(config: &toml::Table, key: &str) -> String {
    let env = setting_env(key).expect("validated setting");
    let override_value = std::env::var(env).ok().filter(|value| !value.trim().is_empty());
    let (source, value) = if let Some(value) = override_value {
        ("environment", value)
    } else if config.contains_key(key) {
        let value = if key == "worktree_per_session" {
            match config.get(key) {
                Some(toml::Value::Boolean(true)) => "on".into(),
                Some(toml::Value::Boolean(false)) => "off".into(),
                Some(toml::Value::String(value)) if !value.trim().is_empty() => value.clone(),
                _ => "on".into(),
            }
        } else { doxa_state::raw_setting(None, config, key) };
        ("config.toml", value)
    } else {
        ("default", if key == "linger_secs" { "120".into() } else { "1".into() })
    };
    let display = if key == "worktree_per_session" {
        if matches!(value.trim().to_ascii_lowercase().as_str(), "0" | "false" | "no" | "off") { "off" }
        else { "on" }
    } else { value.trim() };
    format!("{} ({source})", safe_report_value(display))
}

pub fn settings_report() -> io::Result<String> {
    let config = doxa_state::load_config_checked(&doxa_home()?.join("config.toml"))?;
    Ok(format!("native settings · environment > config.toml > default (launch flags can override)\nlinger_secs: {}\nworktree_per_session: {}\n\nChange with `doxa settings set KEY VALUE`; remove with `doxa settings unset KEY`. These affect new sessions; running sessions keep their launch settings.",
        effective_setting(&config, "linger_secs"),
        effective_setting(&config, "worktree_per_session")))
}

fn edit_setting(path: &Path, key: &str, value: Option<&str>, env_override: Option<&str>) -> io::Result<()> {
    let env = setting_env(key).ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput,
        "setting must be linger_secs or worktree_per_session"))?;
    if env_override.is_some_and(|value| !value.trim().is_empty()) {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied,
            format!("{env} overrides config.toml; unset it before changing {key}")));
    }
    let parsed = match (key, value) {
        (_, None) => None,
        ("linger_secs", Some(value)) => {
            let seconds: f64 = value.parse().map_err(|_| io::Error::new(io::ErrorKind::InvalidInput,
                "linger_secs must be a nonnegative finite number"))?;
            if !seconds.is_finite() || !(0.0..=crate::launch::MAX_LINGER_SECS).contains(&seconds) {
                return Err(io::Error::new(io::ErrorKind::InvalidInput,
                    "linger_secs must be a finite number between 0 and 31536000 seconds"));
            }
            Some(toml::Value::Float(seconds))
        }
        ("worktree_per_session", Some("on" | "true" | "1")) => Some(toml::Value::Boolean(true)),
        ("worktree_per_session", Some("off" | "false" | "0")) => Some(toml::Value::Boolean(false)),
        _ => return Err(io::Error::new(io::ErrorKind::InvalidInput,
            "worktree_per_session accepts on or off")),
    };
    doxa_state::update_config(path, |stored| {
        if let Some(value) = parsed { stored.insert(key.into(), value); }
        else { stored.remove(key); }
        Ok(())
    })
}

pub fn settings_change(key: &str, value: Option<&str>) -> io::Result<String> {
    let env = setting_env(key).ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput,
        "setting must be linger_secs or worktree_per_session"))?;
    edit_setting(&doxa_home()?.join("config.toml"), key, value,
        std::env::var(env).ok().as_deref())?;
    Ok(format!("{key}: {} for new sessions",
        if value.is_some() { "stored" } else { "removed from config.toml" }))
}

fn read_claude_json(path: &Path) -> Option<serde_json::Value> {
    // Plugin registries are external state. Do not follow links, inspect
    // special files, or read unbounded content from them.
    let file = std::fs::OpenOptions::new().read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path).ok()?;
    let metadata = file.metadata().ok()?;
    if !metadata.is_file() || metadata.nlink() != 1 || metadata.len() > 1024 * 1024 {
        return None;
    }
    let mut data = Vec::new();
    file.take(1024 * 1024 + 1).read_to_end(&mut data).ok()?;
    if data.len() > 1024 * 1024 { return None; }
    serde_json::from_slice(&data).ok()
}

fn safe_plugin_name(name: &str) -> bool {
    !name.is_empty() && name.len() <= 128 && name != ".."
        && name.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"._-@".contains(&byte))
}

fn plugin_rows(installed: &serde_json::Value, settings: &serde_json::Value) -> Vec<String> {
    let enabled = settings.get("enabledPlugins").and_then(serde_json::Value::as_object);
    let mut rows = installed.get("plugins").and_then(serde_json::Value::as_object)
        .into_iter().flat_map(|plugins| plugins.iter())
        .filter(|(name, entries)| safe_plugin_name(name)
            && entries.as_array().is_some_and(|entries| !entries.is_empty()))
        .map(|(name, _)| {
            let state = if enabled.and_then(|settings| settings.get(name)) == Some(&serde_json::Value::Bool(true)) {
                "enabled"
            } else { "disabled" };
            format!("{name}: {state} in Claude Code")
        }).collect::<Vec<_>>();
    rows.sort();
    rows
}

pub fn plugins_report() -> io::Result<String> {
    let base = std::env::var_os("CLAUDE_CONFIG_DIR").filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|value| PathBuf::from(value).join(".claude")))
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "CLAUDE_CONFIG_DIR and HOME are unset"))?;
    let installed = read_claude_json(&base.join("plugins/installed_plugins.json"))
        .unwrap_or(serde_json::Value::Null);
    let settings = read_claude_json(&base.join("settings.json"))
        .unwrap_or(serde_json::Value::Null);
    let rows = plugin_rows(&installed, &settings);
    if rows.is_empty() { Ok("no Claude Code plugins found".into()) }
    else { Ok(format!("Claude Code plugins (inventory only)\n{}", rows.join("\n"))) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn probe_uses_exit_status_and_never_exposes_output() {
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("probe");
        fs::write(&script, "#!/bin/sh\nprintf 'secret credential\\n'\nprintf 'secret error\\n' >&2\n[ \"$1\" = ok ]\n").unwrap();
        fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
        // CI may mount its temporary directory noexec. The probe contract is
        // exit status and output isolation, not executing a temporary file.
        let script = script.to_str().unwrap();
        assert_eq!(probe(Path::new("/bin/sh"), &[script, "ok"], Duration::from_secs(5)), AuthState::Authenticated);
        assert_eq!(probe(Path::new("/bin/sh"), &[script, "fail"], Duration::from_secs(5)), AuthState::Unauthenticated);
    }

    #[test]
    fn probe_times_out_and_reaps_child() {
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("probe");
        fs::write(&script, "#!/bin/sh\nsleep 5\n").unwrap();
        fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
        let start = Instant::now();
        assert_eq!(probe(Path::new("/bin/sh"), &[script.to_str().unwrap()], Duration::from_millis(10)), AuthState::TimedOut);
        assert!(start.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn preference_shows_precedence() {
        let mut config = toml::Table::new();
        config.insert("model".into(), toml::Value::String("stored".into()));
        assert_eq!(preference(&config, "model", "DOXA_TEST_MISSING_PREFERENCE"), "stored (config.toml)");
        assert_eq!(preference(&config, "missing", "DOXA_TEST_MISSING_PREFERENCE"), "(CLI default)");
    }

    #[test]
    fn native_settings_store_only_valid_active_preferences() {
        let dir = tempfile::tempdir().unwrap();
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let path = dir.path().join("config.toml");
        fs::write(&path, "future = 'keep'\n").unwrap();
        edit_setting(&path, "linger_secs", Some("42.5"), None).unwrap();
        edit_setting(&path, "worktree_per_session", Some("off"), None).unwrap();
        let config = doxa_state::load_config_checked(&path).unwrap();
        assert_eq!(config["linger_secs"].as_float(), Some(42.5));
        assert_eq!(config["worktree_per_session"].as_bool(), Some(false));
        assert_eq!(config["future"].as_str(), Some("keep"));
        assert_eq!(effective_setting(&config, "worktree_per_session"), "off (config.toml)");
        let mut malformed = config.clone();
        malformed.insert("worktree_per_session".into(), toml::Value::Integer(0));
        assert_eq!(effective_setting(&malformed, "worktree_per_session"), "on (config.toml)");
        assert!(edit_setting(&path, "linger_secs", Some("NaN"), None).is_err());
        assert!(edit_setting(&path, "linger_secs", Some("1e308"), None).is_err());
        assert!(edit_setting(&path, "worktree_per_session", Some("maybe"), None).is_err());
        assert!(edit_setting(&path, "linger_secs", Some("10"), Some("7")).is_err());
        assert_eq!(doxa_state::load_config_checked(&path).unwrap(), config);
        edit_setting(&path, "linger_secs", None, None).unwrap();
        assert!(doxa_state::load_config_checked(&path).unwrap().get("linger_secs").is_none());
    }

    #[test]
    fn report_values_cannot_inject_terminal_control_sequences() {
        assert_eq!(safe_report_value("model\u{1b}[31m\nnext\u{202e}"), "model[31mnext");
    }

    #[test]
    fn plugin_inventory_uses_registry_names_without_paths_or_metadata() {
        let installed = serde_json::json!({"plugins": {
            "safe@market": [{"installPath": "/private/secret"}],
            "../unsafe": [{}],
            "empty@market": []
        }});
        let settings = serde_json::json!({"enabledPlugins": {"safe@market": true}});
        assert_eq!(plugin_rows(&installed, &settings), vec!["safe@market: enabled in Claude Code"]);
    }
}

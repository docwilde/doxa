//! Carrier configuration mirrors DOXA's sticky store and the narrow shared
//! Claude capacity export without mutating the process environment.
use crate::LoreError;
use lore_core::config::Config;
use serde_json::Value;
use std::collections::HashMap;
use std::fs;
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::time::Duration;

pub(crate) fn resolve(timeout: Duration) -> Result<Config, LoreError> {
    let mut config = Config::from_env(timeout).map_err(|error| LoreError::Remote(error.code()))?;
    if std::env::var("LORE_ROOT").ok().is_none_or(|value| value.trim().is_empty()) {
        let home = std::env::var_os("DOXA_HOME").filter(|value| !value.is_empty()).map(PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|value| PathBuf::from(value).join(".doxa")))
            .ok_or(LoreError::Unavailable)?;
        let table = doxa_state::load_config(&home.join("config.toml"));
        if let Some(root) = table.get("lore_root").and_then(|value| value.as_str()).filter(|value| !value.is_empty()) {
            let root = PathBuf::from(root);
            if !root.is_absolute() { return Err(LoreError::InvalidFrame); }
            config.root = root;
            if std::env::var_os("LORE_SKILLS_DIR").is_none() {
                let default_root = std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".claude/lore"));
                config.skills = if default_root.as_ref() == Some(&config.root) {
                    default_root.unwrap().parent().unwrap().join("skills")
                } else { config.root.join("skills") };
            }
        }
    }
    let settings = std::env::var("CLAUDE_CONFIG_DIR").ok().filter(|value| !value.trim().is_empty()).map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".claude")))
        .map(|path| path.join("settings.json"));
    let shared = settings.as_deref().map(shared_caps).unwrap_or_default();
    for (name, target) in [("LORE_MEMORY_CAP", &mut config.project_cap), ("LORE_USER_CAP", &mut config.user_cap),
        ("LORE_MACHINE_CAP", &mut config.machine_cap)] {
        if std::env::var_os(name).is_none() {
            if let Some(value) = shared.get(name) { *target = *value; }
        }
    }
    Ok(config)
}

fn shared_caps(path: &Path) -> HashMap<String, usize> {
    let read = || -> Option<Value> {
        let file = fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path).ok()?;
        let meta = file.metadata().ok()?;
        if !meta.is_file() || meta.uid() != unsafe { libc::geteuid() } || meta.nlink() != 1 || meta.len() > 1024 * 1024 { return None; }
        let mut bytes = Vec::new();
        file.take(1024 * 1024 + 1).read_to_end(&mut bytes).ok()?;
        if bytes.len() > 1024 * 1024 { return None; }
        serde_json::from_slice(&bytes).ok()
    };
    let Some(value) = read() else { return HashMap::new(); };
    ["LORE_MEMORY_CAP", "LORE_USER_CAP", "LORE_MACHINE_CAP"].into_iter().filter_map(|name| {
        let raw = value["env"][name].as_str()?;
        if raw.is_empty() || raw.len() > 7 || !raw.bytes().all(|byte| byte.is_ascii_digit()) { return None; }
        let cap = raw.parse::<usize>().ok().filter(|cap| (0..=1024*1024).contains(cap))?;
        Some((name.to_owned(), cap))
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::ffi::OsStrExt;
    #[test]
    fn shared_limits_admit_only_bounded_integer_capacity_fields() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("settings.json");
        fs::write(&path, r#"{"env":{"LORE_USER_CAP":"1234","LORE_MEMORY_CAP":"0","LORE_MACHINE_CAP":9000,"TOKEN":"secret"}}"#).unwrap();
        assert_eq!(shared_caps(&path), HashMap::from([("LORE_USER_CAP".into(), 1234), ("LORE_MEMORY_CAP".into(), 0)]));
        fs::remove_file(&path).unwrap();
        let name = std::ffi::CString::new(path.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        assert!(shared_caps(&path).is_empty());
        fs::remove_file(&path).unwrap();
        let outside = dir.path().join("outside");
        fs::write(&outside, r#"{"env":{"LORE_USER_CAP":"9000"}}"#).unwrap();
        std::os::unix::fs::symlink(&outside, &path).unwrap();
        assert!(shared_caps(&path).is_empty());
    }
}

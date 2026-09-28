//! One-direction CLI auth and approved artifact snapshots. Host auth is never written.
use serde_json::Value;
use std::{
    fs::{self, OpenOptions},
    io::{self, Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
};
fn unsafe_path() -> io::Error {
    io::Error::new(
        io::ErrorKind::PermissionDenied,
        "unsafe Claude isolation path",
    )
}
pub fn config_dir() -> PathBuf {
    std::env::var_os("DOXA_HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".doxa")
        })
        .join("claude-cli")
}
pub fn user_config_base() -> PathBuf {
    std::env::var_os("CLAUDE_CONFIG_DIR")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".claude")
        })
}
fn directory(path: &Path) -> io::Result<()> {
    if !path.is_absolute() {
        return Err(unsafe_path());
    }
    if !path.exists() {
        fs::create_dir_all(path)?;
    }
    let m = fs::symlink_metadata(path)?;
    if !m.is_dir() || m.uid() != unsafe { libc::geteuid() } {
        return Err(unsafe_path());
    }
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
}
fn bytes(path: &Path, limit: u64) -> io::Result<Vec<u8>> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let m = file.metadata()?;
    if !m.is_file() || m.uid() != unsafe { libc::geteuid() } || m.nlink() != 1 || m.len() > limit {
        return Err(unsafe_path());
    }
    let mut bytes = Vec::new();
    file.take(limit + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit {
        return Err(unsafe_path());
    }
    Ok(bytes)
}
fn write(path: &Path, data: &[u8]) -> io::Result<()> {
    directory(path.parent().ok_or_else(unsafe_path)?)?;
    let mut file = tempfile::NamedTempFile::new_in(path.parent().unwrap())?;
    file.as_file()
        .set_permissions(fs::Permissions::from_mode(0o600))?;
    file.write_all(data)?;
    file.as_file().sync_all()?;
    file.persist(path).map_err(|e| e.error)?;
    Ok(())
}
fn oauth(raw: &[u8]) -> bool {
    serde_json::from_slice::<Value>(raw).ok().is_some_and(|v| {
        ["accessToken", "refreshToken"].iter().any(|k| {
            v["claudeAiOauth"][k]
                .as_str()
                .is_some_and(|s| !s.trim().is_empty())
        })
    })
}
pub fn sync_credentials(force: bool) -> io::Result<bool> {
    let base = config_dir();
    directory(&base)?;
    if base.join(".doxa-logged-out").exists() {
        return Ok(false);
    }
    let source = user_config_base().join(".credentials.json");
    let source_bytes = match bytes(&source, 1024 * 1024) {
        Ok(v) => v,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(e) => return Err(e),
    };
    let dest = base.join(".credentials.json");
    if !force {
        if let (Ok(a), Ok(b), Ok(current)) = (
            fs::symlink_metadata(&source),
            fs::symlink_metadata(&dest),
            bytes(&dest, 1024 * 1024),
        ) {
            if b.modified()? >= a.modified()? && (oauth(&current) || !oauth(&source_bytes)) {
                return Ok(false);
            }
        }
    }
    write(&dest, &source_bytes)?;
    Ok(true)
}
fn copy_tree(
    source: &Path,
    dest: &Path,
    plugin: bool,
    depth: usize,
    budget: &mut u64,
) -> io::Result<()> {
    if depth > 24 {
        return Err(unsafe_path());
    }
    directory(dest)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let name = entry.file_name();
        let name_text = name.to_string_lossy();
        if name_text == ".git"
            || plugin
                && matches!(
                    name_text.as_ref(),
                    "hooks" | "hooks.json" | ".mcp.json" | "mcp.json"
                )
        {
            continue;
        }
        let src = entry.path();
        let dst = dest.join(name);
        let real = fs::canonicalize(&src)?;
        let m = fs::metadata(&real)?;
        if m.uid() != unsafe { libc::geteuid() } {
            return Err(unsafe_path());
        }
        if m.is_dir() {
            copy_tree(&real, &dst, plugin, depth + 1, budget)?;
        } else if m.is_file() {
            let data = bytes(&real, 4 * 1024 * 1024)?;
            *budget = budget
                .checked_sub(data.len() as u64)
                .ok_or_else(unsafe_path)?;
            write(&dst, &data)?;
            fs::set_permissions(&dst, fs::Permissions::from_mode(0o600 | (m.mode() & 0o111)))?;
        }
    }
    Ok(())
}
fn snapshot(source: &Path, dest: &Path, plugin: bool) -> io::Result<()> {
    let parent = dest.parent().ok_or_else(unsafe_path)?;
    directory(parent)?;
    let staging = tempfile::Builder::new()
        .prefix(".snapshot-")
        .tempdir_in(parent)?;
    copy_tree(source, staging.path(), plugin, 0, &mut (64 * 1024 * 1024))?;
    write(&staging.path().join(".doxa-skills-snapshot"), b"")?;
    if plugin {
        let path = staging.path().join(".claude-plugin/plugin.json");
        if path.exists() {
            let mut manifest: Value =
                serde_json::from_slice(&bytes(&path, 65536)?).map_err(|_| unsafe_path())?;
            let object = manifest.as_object_mut().ok_or_else(unsafe_path)?;
            for key in ["hooks", "mcpServers", "lspServers"] {
                object.remove(key);
            }
            write(&path, &serde_json::to_vec(&manifest)?)?;
        }
    }
    if let Ok(m) = fs::symlink_metadata(dest) {
        if m.file_type().is_symlink() {
            fs::remove_file(dest)?;
        } else if m.is_dir() && dest.join(".doxa-skills-snapshot").exists() {
            fs::remove_dir_all(dest)?;
        } else {
            return Err(unsafe_path());
        }
    }
    fs::rename(staging.path(), dest)?;
    Ok(())
}
pub fn prepare() -> io::Result<PathBuf> {
    let base = config_dir();
    directory(&base)?;
    write(&base.join("settings.json"), b"{}\n")?;
    sync_credentials(false)?;
    let source = user_config_base().join("skills");
    if source.is_dir() {
        snapshot(&source, &base.join("skills"), false)?;
    }
    Ok(base)
}
pub fn adopted_plugins() -> io::Result<Vec<PathBuf>> {
    if !adoption_enabled() {
        return Ok(vec![]);
    }
    let base = user_config_base();
    let installed: Value = serde_json::from_slice(&bytes(
        &base.join("plugins/installed_plugins.json"),
        1024 * 1024,
    )?)?;
    let settings: Value = serde_json::from_slice(&bytes(&base.join("settings.json"), 65536)?)?;
    let mut result = Vec::new();
    for (key, rows) in installed["plugins"].as_object().into_iter().flatten() {
        if !safe_scope(key) || key == "lore@lore" || settings["enabledPlugins"][key] != true {
            continue;
        }
        let Some(source) = rows[0]["installPath"].as_str().map(PathBuf::from) else {
            continue;
        };
        if !source.is_absolute()
            || !["commands", "skills", "agents"]
                .iter()
                .any(|name| source.join(name).is_dir())
        {
            continue;
        }
        let dest = config_dir().join("adopted-plugins").join(key);
        if snapshot(&source, &dest, true).is_ok() {
            result.push(dest);
        }
    }
    Ok(result)
}
pub fn mark_logged_out() -> io::Result<()> {
    let base = config_dir();
    directory(&base)?;
    write(&base.join(".doxa-logged-out"), b"")?;
    match fs::remove_file(base.join(".credentials.json")) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e),
    }
}
pub fn mark_logged_in() -> io::Result<bool> {
    let marker = config_dir().join(".doxa-logged-out");
    if marker.exists() {
        let time = fs::metadata(&marker)?.modified()?;
        let dest = config_dir().join(".credentials.json");
        if !bytes(&dest, 1024 * 1024).is_ok_and(|v| oauth(&v))
            || fs::metadata(&dest)?.modified()? <= time
        {
            let source = user_config_base().join(".credentials.json");
            if fs::metadata(&source)?.modified()? <= time {
                return Ok(false);
            }
        }
        fs::remove_file(marker)?;
    }
    sync_credentials(false)?;
    Ok(bytes(&config_dir().join(".credentials.json"), 1024 * 1024).is_ok_and(|v| oauth(&v)))
}

/// Same saved preference used by the native UI; staging remains opt-in.
pub fn adoption_enabled() -> bool {
    let config = doxa_state::load_config(&config_dir().parent().unwrap().join("config.toml"));
    let raw = doxa_state::raw_setting(
        std::env::var("DOXA_ADOPT_PLUGINS").ok().as_deref(),
        &config,
        "adopt_plugins",
    );
    !raw.trim().is_empty()
        && !matches!(
            raw.trim().to_ascii_lowercase().as_str(),
            "0" | "false" | "no" | "off"
        )
}

/// Read-only approved plugin commands from the user registry. This performs
/// no SDK initialization, plugin code execution, staging, or hook discovery.
pub fn plugin_commands(mut cancelled: impl FnMut() -> bool) -> io::Result<Vec<Value>> {
    if !adoption_enabled() {
        return Ok(Vec::new());
    }
    let base = user_config_base();
    let installed: Value = match bytes(&base.join("plugins/installed_plugins.json"), 1024 * 1024) {
        Ok(raw) => serde_json::from_slice(&raw)?,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error),
    };
    let settings: Value = match bytes(&base.join("settings.json"), 65536) {
        Ok(raw) => serde_json::from_slice(&raw)?,
        Err(error) if error.kind() == io::ErrorKind::NotFound => Value::Null,
        Err(error) => return Err(error),
    };
    let mut output = Vec::new();
    for (key, rows) in installed["plugins"].as_object().into_iter().flatten() {
        if cancelled() {
            return Err(io::Error::new(
                io::ErrorKind::Interrupted,
                "Plugin inventory cancelled",
            ));
        }
        if !safe_scope(key) || key == "lore@lore" || settings["enabledPlugins"][key] != true {
            continue;
        }
        let Some(source) = rows[0]["installPath"]
            .as_str()
            .map(PathBuf::from)
            .filter(|p| p.is_absolute())
        else {
            continue;
        };
        let plugin = key.split('@').next().unwrap_or(key);
        for entry in fs::read_dir(source.join("commands"))
            .into_iter()
            .flatten()
            .flatten()
        {
            if cancelled() {
                return Err(io::Error::new(
                    io::ErrorKind::Interrupted,
                    "Plugin inventory cancelled",
                ));
            }
            let path = entry.path();
            if path.extension().is_none_or(|v| v != "md") {
                continue;
            }
            let Some(name) = path
                .file_stem()
                .and_then(|v| v.to_str())
                .filter(|v| safe_scope(v))
            else {
                continue;
            };
            let raw = match fs::canonicalize(&path).and_then(|path| bytes(&path, 65536)) {
                Ok(raw) => raw,
                Err(_) => continue,
            };
            let text = String::from_utf8(raw)
                .map_err(|_| io::Error::other("Invalid plugin command text"))?;
            let (summary, usage) = front_matter(&text);
            let full = format!("/{plugin}:{name}");
            if full.len() > 128 {
                continue;
            }
            output.push(
                serde_json::json!({"name":full,"summary":summary,"usage":usage,"plugin":key}),
            );
            if output.len() > 100 {
                return Err(io::Error::other(
                    "Plugin command inventory exceeds 100 entries",
                ));
            }
        }
    }
    output.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
    Ok(output)
}
fn safe_scope(key: &str) -> bool {
    !key.is_empty()
        && key.len() <= 128
        && !key.starts_with('.')
        && !key.contains("..")
        && key
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.@".contains(&b))
}
fn front_matter(text: &str) -> (String, String) {
    let mut description = String::new();
    let mut usage = String::new();
    if !text.starts_with("---\n") {
        return (description, usage);
    }
    for line in text[4..].lines() {
        if line.trim() == "---" {
            break;
        }
        if let Some((key, value)) = line.split_once(':') {
            let value = value.trim().trim_matches(['\'', '"']);
            if value.len() > 1024 || value.chars().any(char::is_control) {
                continue;
            }
            match key.trim() {
                "description" => description = value.into(),
                "argument-hint" => usage = value.into(),
                _ => {}
            }
        }
    }
    (description, usage)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn plugin_snapshot_strips_executing_channels_and_copies_links_one_direction() {
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("plugin");
        let target = dir.path().join("isolated");
        fs::create_dir_all(source.join(".claude-plugin")).unwrap();
        fs::create_dir_all(source.join("hooks")).unwrap();
        fs::create_dir_all(source.join("scripts")).unwrap();
        fs::write(source.join(".claude-plugin/plugin.json"),br#"{"name":"safe","hooks":"arbitrary-command","mcpServers":{"foreign":{}},"lspServers":{},"description":"approved commands"}"#).unwrap();
        fs::write(source.join("hooks/hooks.json"), "bad").unwrap();
        fs::write(source.join(".mcp.json"), "bad").unwrap();
        fs::write(source.join("scripts/run"), "#!/bin/sh\nexit 0\n").unwrap();
        fs::set_permissions(
            source.join("scripts/run"),
            fs::Permissions::from_mode(0o700),
        )
        .unwrap();
        let approved = dir.path().join("approved.md");
        fs::write(&approved, "approved skill").unwrap();
        std::os::unix::fs::symlink(&approved, source.join("approved.md")).unwrap();
        snapshot(&source, &target, true).unwrap();
        assert!(!target.join("hooks").exists());
        assert!(!target.join(".mcp.json").exists());
        let manifest: Value =
            serde_json::from_slice(&fs::read(target.join(".claude-plugin/plugin.json")).unwrap())
                .unwrap();
        assert!(manifest.get("hooks").is_none());
        assert!(manifest.get("mcpServers").is_none());
        assert!(manifest.get("lspServers").is_none());
        assert_eq!(manifest["description"], "approved commands");
        assert!(fs::metadata(target.join("scripts/run")).unwrap().mode() & 0o100 != 0);
        assert!(!fs::symlink_metadata(target.join("approved.md"))
            .unwrap()
            .file_type()
            .is_symlink());
        fs::write(target.join("approved.md"), "model changed copy").unwrap();
        assert_eq!(fs::read_to_string(&approved).unwrap(), "approved skill");
        snapshot(&source, &target, true).unwrap();
        assert_eq!(
            fs::read_to_string(target.join("approved.md")).unwrap(),
            "approved skill"
        );
    }
    #[test]
    fn snapshot_never_deletes_unowned_directory_or_reads_special_file() {
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("source");
        let target = dir.path().join("target");
        fs::create_dir(&source).unwrap();
        fs::create_dir(&target).unwrap();
        fs::write(target.join("not-ours"), "keep").unwrap();
        assert!(snapshot(&source, &target, false).is_err());
        assert_eq!(fs::read_to_string(target.join("not-ours")).unwrap(), "keep");
        assert!(bytes(Path::new("/dev/zero"), 10).is_err());
        assert!(!safe_scope("safe..collision"));
        assert_eq!(
            front_matter("---\ndescription: 'A useful command'\nargument-hint: TASK\n---\nRun it"),
            ("A useful command".into(), "TASK".into())
        );
    }
}

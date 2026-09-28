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
    let raw = std::env::var("DOXA_ADOPT_PLUGINS").unwrap_or_default();
    if raw.is_empty()
        || matches!(
            raw.to_ascii_lowercase().as_str(),
            "0" | "false" | "no" | "off"
        )
    {
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
        if key.len() > 128
            || key.starts_with('.')
            || !key
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"-_.@".contains(&b))
            || key == "lore@lore"
            || settings["enabledPlugins"][key] != true
        {
            continue;
        }
        let Some(source) = rows[0]["installPath"].as_str().map(PathBuf::from) else {
            continue;
        };
        if !source.is_absolute() {
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

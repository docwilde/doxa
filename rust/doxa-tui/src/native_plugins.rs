//! Owner-approved, data-only TUI contributions. This module never loads code.
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::ffi::CString;
use std::fs::File;
use std::io::{self, Read};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

pub const API_VERSION: u32 = 1;
const MAX_CONFIG: u64 = 1024 * 1024;
const MAX_MANIFEST: u64 = 16 * 1024;
const MAX_PLUGINS: usize = 16;
const MAX_COMMANDS: usize = 8;

#[derive(Clone, Debug)]
pub struct Command {
    pub name: String,
    pub summary: String,
    pub body: String,
    pub plugin: String,
    pub source: PathBuf,
    pub sha256: String,
    pub inode: u64,
    pub device: u64,
}

#[derive(Debug, Default)]
pub struct Inventory {
    pub commands: Vec<Command>,
    pub failures: Vec<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    api_version: u32,
    name: String,
    version: String,
    commands: Vec<ManifestCommand>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ManifestCommand {
    name: String,
    summary: String,
    body: String,
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

fn private(meta: &std::fs::Metadata, directory: bool, private_mode: bool) -> io::Result<()> {
    let kind_ok = if directory { meta.is_dir() } else { meta.is_file() && meta.nlink() == 1 };
    if !kind_ok || meta.uid() != unsafe { libc::geteuid() }
        || (private_mode && meta.mode() & 0o077 != 0) {
        return Err(invalid("native plugin state must be owner-owned, private and regular"));
    }
    Ok(())
}

fn open_dir(path: &Path) -> io::Result<File> {
    let path = CString::new(path.as_os_str().as_bytes()).map_err(|_| invalid("NUL in plugin path"))?;
    let fd = unsafe { libc::open(path.as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
    if fd < 0 { return Err(io::Error::last_os_error()); }
    let file = unsafe { File::from_raw_fd(fd) };
    private(&file.metadata()?, true, false)?;
    Ok(file)
}

fn open_child(directory: &File, name: &str, is_dir: bool, private_mode: bool, limit: u64) -> io::Result<(File, Vec<u8>)> {
    let name = CString::new(name).map_err(|_| invalid("NUL in plugin filename"))?;
    let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK
        | if is_dir { libc::O_DIRECTORY } else { 0 };
    let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(), flags) };
    if fd < 0 { return Err(io::Error::last_os_error()); }
    let mut file = unsafe { File::from_raw_fd(fd) };
    let meta = file.metadata()?;
    private(&meta, is_dir, private_mode)?;
    if is_dir { return Ok((file, Vec::new())); }
    if meta.len() > limit { return Err(invalid("native plugin file exceeds size limit")); }
    let mut bytes = Vec::new();
    file.by_ref().take(limit + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit { return Err(invalid("native plugin file exceeds size limit")); }
    Ok((file, bytes))
}

fn identifier(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty() && bytes.len() <= 48 && bytes[0].is_ascii_lowercase()
        && bytes.iter().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'-')
}

fn plain(value: &str, max: usize, multiline: bool) -> bool {
    !value.is_empty() && value.len() <= max && value.chars().all(|c| {
        (multiline && c == '\n') || (!c.is_control()
            && !matches!(c, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}'))
    })
}

fn parse_manifest(bytes: &[u8], expected: &str, source: &Path, meta: &std::fs::Metadata, reserved: &[&str]) -> io::Result<Vec<Command>> {
    let text = std::str::from_utf8(bytes).map_err(|_| invalid("plugin manifest must be UTF-8"))?;
    let manifest: Manifest = toml::from_str(text).map_err(|error| invalid(format!("invalid plugin manifest: {error}")))?;
    if manifest.api_version != API_VERSION {
        return Err(invalid(format!("plugin API {} does not match DOXA API {API_VERSION}", manifest.api_version)));
    }
    if manifest.name != expected || !identifier(&manifest.name) {
        return Err(invalid("plugin identity does not match its allowlisted filename"));
    }
    if !plain(&manifest.version, 64, false) {
        return Err(invalid("invalid plugin version"));
    }
    if manifest.commands.is_empty() || manifest.commands.len() > MAX_COMMANDS {
        return Err(invalid("plugin must contribute 1–8 commands"));
    }
    let digest = format!("{:x}", Sha256::digest(bytes));
    let mut commands = Vec::new();
    for row in manifest.commands {
        let Some(suffix) = row.name.strip_prefix(&format!("/{expected}:")) else {
            return Err(invalid("native command must use /plugin:command namespace"));
        };
        if !identifier(suffix) || !plain(&row.summary, 100, false) || !plain(&row.body, 4096, true) {
            return Err(invalid("invalid native command name or display text"));
        }
        if reserved.contains(&row.name.as_str()) {
            return Err(invalid("native command conflicts with a built-in DOXA command"));
        }
        if commands.iter().any(|command: &Command| command.name == row.name) {
            return Err(invalid("duplicate native command"));
        }
        commands.push(Command { name: row.name, summary: row.summary, body: row.body,
            plugin: manifest.name.clone(), source: source.to_path_buf(), sha256: digest.clone(),
            inode: meta.ino(), device: meta.dev() });
    }
    Ok(commands)
}

/// Read only names explicitly listed in the owner's config. No directory scan,
/// repository lookup, executable field, script or dynamic library is accepted.
pub fn load(home: &Path, reserved: &[&str]) -> io::Result<Inventory> {
    let home_dir = open_dir(home)?;
    let (config_file, config_bytes) = match open_child(&home_dir, "config.toml", false, false, MAX_CONFIG) {
        Ok(value) => value,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Inventory::default()),
        Err(error) => return Err(error),
    };
    let config = std::str::from_utf8(&config_bytes).map_err(|_| invalid("DOXA config must be UTF-8"))?
        .parse::<toml::Table>().map_err(|error| invalid(format!("invalid DOXA config: {error}")))?;
    let Some(allowlist) = config.get("native_plugins") else { return Ok(Inventory::default()); };
    let allowlist = allowlist.as_array().ok_or_else(|| invalid("native_plugins must be an array of names"))?;
    if allowlist.len() > MAX_PLUGINS { return Err(invalid("too many native plugins")); }
    if allowlist.is_empty() { return Ok(Inventory::default()); }
    private(&home_dir.metadata()?, true, true)?;
    private(&config_file.metadata()?, false, true)?;
    if home.canonicalize()?.ancestors().any(|ancestor| ancestor.join(".git").exists()) {
        return Err(invalid("native plugin home must be outside a working repository"));
    }
    let (directory, _) = open_child(&home_dir, "native-plugins", true, true, 0)?;
    let mut inventory = Inventory::default();
    let mut seen = std::collections::HashSet::new();
    for value in allowlist {
        let Some(name) = value.as_str() else { return Err(invalid("native_plugins must contain only names")); };
        if !identifier(name) || !seen.insert(name) { return Err(invalid("invalid or duplicate native plugin name")); }
        let filename = format!("{name}.toml");
        let source = home.join("native-plugins").join(&filename);
        let result = open_child(&directory, &filename, false, true, MAX_MANIFEST).and_then(|(file, bytes)|
            parse_manifest(&bytes, name, &source, &file.metadata()?, reserved));
        match result {
            Ok(mut commands) => inventory.commands.append(&mut commands),
            Err(error) => inventory.failures.push(format!("{name}: {error}")),
        }
    }
    inventory.commands.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(inventory)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{symlink, PermissionsExt};

    fn fixture() -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        std::fs::create_dir(dir.path().join("native-plugins")).unwrap();
        std::fs::set_permissions(dir.path().join("native-plugins"), std::fs::Permissions::from_mode(0o700)).unwrap();
        dir
    }
    fn write(path: &Path, value: &str) {
        std::fs::write(path, value).unwrap();
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).unwrap();
    }
    const MANIFEST: &str = "api_version = 1\nname = 'demo'\nversion = '1.0'\n[[commands]]\nname = '/demo:status'\nsummary = 'Show status'\nbody = 'All systems ready'\n";

    #[test]
    fn only_owner_allowlisted_data_is_loaded_with_opened_file_provenance() {
        let dir = fixture();
        let plugin = dir.path().join("native-plugins/demo.toml");
        write(&plugin, MANIFEST);
        write(&dir.path().join("native-plugins/hidden.toml"), MANIFEST);
        write(&dir.path().join("config.toml"), "native_plugins = ['demo']\n");
        let inventory = load(dir.path(), &[]).unwrap();
        assert!(inventory.failures.is_empty());
        assert_eq!(inventory.commands.len(), 1);
        let row = &inventory.commands[0];
        assert_eq!(row.name, "/demo:status");
        assert_eq!(row.source, plugin);
        assert_eq!(row.sha256, format!("{:x}", Sha256::digest(MANIFEST.as_bytes())));
        assert_eq!(row.inode, std::fs::metadata(&row.source).unwrap().ino());
        let collision = load(dir.path(), &["/demo:status"]).unwrap();
        assert!(collision.commands.is_empty());
        assert!(collision.failures[0].contains("built-in"));
        write(&dir.path().join("config.toml"), "");
        assert!(load(dir.path(), &[]).unwrap().commands.is_empty());
    }

    #[test]
    fn rejects_symlinks_loose_permissions_code_fields_and_api_mismatch() {
        let dir = fixture();
        write(&dir.path().join("config.toml"), "native_plugins = ['demo']\n");
        let plugin = dir.path().join("native-plugins/demo.toml");
        write(&plugin, MANIFEST);
        std::fs::set_permissions(&plugin, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert_eq!(load(dir.path(), &[]).unwrap().failures.len(), 1);
        std::fs::remove_file(&plugin).unwrap();
        symlink(dir.path().join("outside.toml"), &plugin).unwrap();
        assert_eq!(load(dir.path(), &[]).unwrap().failures.len(), 1);
        std::fs::remove_file(&plugin).unwrap();
        write(&plugin, &MANIFEST.replace("api_version = 1", "api_version = 2"));
        assert!(load(dir.path(), &[]).unwrap().failures[0].contains("API 2"));
        write(&plugin, &format!("{MANIFEST}exec = '/bin/sh'\n"));
        assert_eq!(load(dir.path(), &[]).unwrap().commands.len(), 0);
        assert!(!load(dir.path(), &[]).unwrap().failures.is_empty());
        std::fs::set_permissions(dir.path().join("config.toml"), std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(load(dir.path(), &[]).is_err(), "the allowlist itself must be private");
        std::fs::set_permissions(dir.path().join("config.toml"), std::fs::Permissions::from_mode(0o600)).unwrap();
        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o755)).unwrap();
        assert!(load(dir.path(), &[]).is_err());
    }

    #[test]
    fn invalid_allowlist_fails_closed_before_reading_any_manifest() {
        let dir = fixture();
        write(&dir.path().join("config.toml"), "native_plugins = ['../repo']\n");
        assert!(load(dir.path(), &[]).is_err());
        write(&dir.path().join("config.toml"), "native_plugins = ['demo', 'demo']\n");
        assert!(load(dir.path(), &[]).is_err());
    }

    #[test]
    fn working_repository_cannot_be_a_native_plugin_home() {
        let dir = fixture();
        std::fs::create_dir(dir.path().join(".git")).unwrap();
        write(&dir.path().join("config.toml"), "native_plugins = ['demo']\n");
        write(&dir.path().join("native-plugins/demo.toml"), MANIFEST);
        assert!(load(dir.path(), &[]).unwrap_err().to_string().contains("working repository"));
    }
}

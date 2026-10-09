//! Owner-approved, data-only TUI contributions. The TUI never loads code.
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::ffi::CString;
use std::fs::File;
use std::io::{self, Read};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::time::{Duration, Instant, SystemTime};

pub const API_VERSION: u32 = 1;
pub mod packages;
// Staged worker contract has no production caller until the OS sandbox lands.
#[allow(dead_code)]
pub(crate) mod runner;
// Child lifecycle component is staged without any package execution route.
#[cfg(target_os = "linux")]
#[allow(dead_code)]
pub(crate) mod runner_process;
#[cfg(target_os = "linux")]
pub(crate) mod runner_sandbox;
const MAX_CONFIG: u64 = 1024 * 1024;
const MAX_MANIFEST: u64 = 16 * 1024;
const MAX_PLUGINS: usize = 16;
const MAX_COMMANDS: usize = 8;
const MAX_STATUSES: usize = 8;
const MAX_STATUS_FILE: u64 = 1024;
const STATUS_TIMEOUT: Duration = Duration::from_millis(250);
const STATUS_FAILURE_BUDGET: u8 = 3;

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
    pub statuses: Vec<StatusSpec>,
    pub failures: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct StatusSpec {
    pub plugin: String,
    pub label: String,
    pub refresh_seconds: u64,
    pub source: PathBuf,
    home: PathBuf,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    api_version: u32,
    name: String,
    version: String,
    #[serde(default)]
    commands: Vec<ManifestCommand>,
    status: Option<ManifestStatus>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ManifestStatus {
    producer: String,
    label: String,
    refresh_seconds: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct StatusFile { value: String }

struct Contribution { commands: Vec<Command>, status: Option<StatusSpec> }

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

#[derive(Debug)]
struct StaleFile;

impl std::fmt::Display for StaleFile {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "status owner file is stale")
    }
}

impl std::error::Error for StaleFile {}

fn stale_file() -> io::Error { io::Error::new(io::ErrorKind::InvalidData, StaleFile) }

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

fn open_child(directory: &File, name: &str, is_dir: bool, private_mode: bool, limit: u64) -> io::Result<(File, Vec<u8>, std::fs::Metadata)> {
    let name = CString::new(name).map_err(|_| invalid("NUL in plugin filename"))?;
    let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK
        | if is_dir { libc::O_DIRECTORY } else { 0 };
    let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(), flags) };
    if fd < 0 { return Err(io::Error::last_os_error()); }
    let mut file = unsafe { File::from_raw_fd(fd) };
    let meta = file.metadata()?;
    private(&meta, is_dir, private_mode)?;
    if is_dir { return Ok((file, Vec::new(), meta)); }
    if meta.len() > limit { return Err(invalid("native plugin file exceeds size limit")); }
    let mut bytes = Vec::new();
    file.by_ref().take(limit + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit { return Err(invalid("native plugin file exceeds size limit")); }
    Ok((file, bytes, meta))
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

fn parse_manifest(bytes: &[u8], expected: &str, source: &Path, meta: &std::fs::Metadata, reserved: &[&str]) -> io::Result<Contribution> {
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
    if manifest.commands.len() > MAX_COMMANDS || manifest.commands.is_empty() && manifest.status.is_none() {
        return Err(invalid("plugin must contribute 1–8 commands or one status"));
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
    let status = if let Some(status) = manifest.status {
        if status.producer != "owner-file-v1" || !plain(&status.label, 32, false)
            || !(5..=300).contains(&status.refresh_seconds) {
            return Err(invalid("invalid native status producer, label or refresh interval"));
        }
        let home = source.parent().and_then(Path::parent).ok_or_else(|| invalid("invalid plugin source"))?;
        Some(StatusSpec { plugin: manifest.name.clone(), label: status.label,
            refresh_seconds: status.refresh_seconds,
            source: home.join("native-plugins").join(format!("{expected}.status.toml")),
            home: home.to_path_buf() })
    } else { None };
    Ok(Contribution { commands, status })
}

/// Read only names explicitly listed in the owner's config. No directory scan,
/// repository lookup, executable field, script or dynamic library is accepted.
pub fn load(home: &Path, reserved: &[&str]) -> io::Result<Inventory> {
    let home_dir = open_dir(home)?;
    let (config_file, config_bytes, _) = match open_child(&home_dir, "config.toml", false, false, MAX_CONFIG) {
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
    let (directory, _, _) = open_child(&home_dir, "native-plugins", true, true, 0)?;
    let mut inventory = Inventory::default();
    let mut seen = std::collections::HashSet::new();
    for value in allowlist {
        let Some(name) = value.as_str() else { return Err(invalid("native_plugins must contain only names")); };
        if !identifier(name) || !seen.insert(name) { return Err(invalid("invalid or duplicate native plugin name")); }
        let filename = format!("{name}.toml");
        let source = home.join("native-plugins").join(&filename);
        let result = open_child(&directory, &filename, false, true, MAX_MANIFEST).and_then(|(_, bytes, meta)|
            parse_manifest(&bytes, name, &source, &meta, reserved));
        match result {
            Ok(mut contribution) => {
                if contribution.status.is_some() && inventory.statuses.len() == MAX_STATUSES {
                    inventory.failures.push(format!("{name}: too many native statuses"));
                    continue;
                }
                inventory.commands.append(&mut contribution.commands);
                if let Some(status) = contribution.status { inventory.statuses.push(status); }
            }
            Err(error) => inventory.failures.push(format!("{name}: {error}")),
        }
    }
    inventory.commands.sort_by(|a, b| a.name.cmp(&b.name));
    inventory.statuses.sort_by(|a, b| a.plugin.cmp(&b.plugin));
    Ok(inventory)
}

#[derive(Debug)]
struct StatusRead {
    value: String,
    sha256: String,
    inode: u64,
    device: u64,
    valid_until: Instant,
}

fn read_status(spec: &StatusSpec, observed_bytes: &mut u64) -> io::Result<StatusRead> {
    let home = open_dir(&spec.home)?;
    private(&home.metadata()?, true, true)?;
    if spec.home.canonicalize()?.ancestors().any(|ancestor| ancestor.join(".git").exists()) {
        return Err(invalid("plugin home is inside a repository"));
    }
    let (directory, _, _) = open_child(&home, "native-plugins", true, true, 0)?;
    let filename = format!("{}.status.toml", spec.plugin);
    let (file, bytes, before) = open_child(&directory, &filename, false, true, MAX_STATUS_FILE)?;
    *observed_bytes = bytes.len() as u64;
    let after = file.metadata()?;
    private(&after, false, true)?;
    if before.dev() != after.dev() || before.ino() != after.ino()
        || before.len() != after.len() || before.mtime() != after.mtime()
        || before.mtime_nsec() != after.mtime_nsec()
        || before.ctime() != after.ctime() || before.ctime_nsec() != after.ctime_nsec() {
        return Err(invalid("status file changed during read"));
    }
    let text = std::str::from_utf8(&bytes).map_err(|_| invalid("status must be UTF-8"))?;
    let payload: StatusFile = toml::from_str(text).map_err(|_| invalid("invalid status document"))?;
    if !plain(&payload.value, 96, false) { return Err(invalid("invalid status value")); }
    let age = SystemTime::now().duration_since(after.modified()?).map_err(|_| stale_file())?;
    let lifetime = Duration::from_secs(spec.refresh_seconds * 3);
    if age > lifetime { return Err(stale_file()); }
    Ok(StatusRead { value: payload.value, sha256: format!("{:x}", Sha256::digest(&bytes)),
        inode: before.ino(), device: before.dev(), valid_until: Instant::now() + (lifetime - age) })
}

fn failure_code(error: &io::Error) -> &'static str {
    if error.get_ref().and_then(|inner| inner.downcast_ref::<StaleFile>()).is_some() {
        return "stale_file";
    }
    match error.kind() {
        io::ErrorKind::NotFound => "missing_file",
        io::ErrorKind::PermissionDenied => "unreadable_file",
        io::ErrorKind::InvalidData => "invalid_or_unsafe_file",
        _ => "read_failed",
    }
}

#[derive(Debug)]
struct StatusEntry {
    spec: StatusSpec,
    value: Option<String>,
    sha256: Option<String>,
    inode: Option<(u64, u64)>,
    valid_until: Option<Instant>,
    disabled: bool,
    refreshes: u64,
    failures: u64,
    consecutive_failures: u8,
    bytes_read: u64,
    elapsed_ms: u128,
    last_elapsed_ms: Option<u128>,
    last_error: Option<&'static str>,
    expirations: u64,
    next_due: Instant,
}

impl StatusEntry {
    fn value_at(&self, now: Instant) -> Option<&str> {
        (now <= self.valid_until?).then(|| self.value.as_deref()).flatten()
    }

    fn record(&mut self, result: Result<StatusRead, &'static str>, now: Instant,
        elapsed: Duration, observed_bytes: u64) -> bool {
        let result = result.and_then(|read| {
            if now > read.valid_until { Err("stale_completion") } else { Ok(read) }
        });
        self.refreshes = self.refreshes.saturating_add(1);
        self.bytes_read = self.bytes_read.saturating_add(observed_bytes);
        self.elapsed_ms = self.elapsed_ms.saturating_add(elapsed.as_millis());
        self.last_elapsed_ms = Some(elapsed.as_millis());
        self.next_due = now + Duration::from_secs(self.spec.refresh_seconds);
        match result {
            Ok(read) => {
                self.value = Some(read.value);
                self.sha256 = Some(read.sha256);
                self.inode = Some((read.device, read.inode));
                self.valid_until = Some(read.valid_until);
                self.consecutive_failures = 0;
                self.last_error = None;
            }
            Err(code) => {
                self.value = None;
                self.sha256 = None;
                self.inode = None;
                self.valid_until = None;
                self.failures = self.failures.saturating_add(1);
                self.consecutive_failures = self.consecutive_failures.saturating_add(1);
                self.last_error = Some(code);
                if self.consecutive_failures >= STATUS_FAILURE_BUDGET { self.disabled = true; }
            }
        }
        self.disabled
    }
}

#[derive(Debug)]
struct PendingStatus {
    index: usize,
    started: Instant,
    receiver: Receiver<StatusCompletion>,
}

#[derive(Debug)]
struct StatusCompletion {
    result: Result<StatusRead, &'static str>,
    elapsed: Duration,
    observed_bytes: u64,
}

#[derive(Debug, Default)]
pub struct StatusRuntime {
    entries: Vec<StatusEntry>,
    pending: Option<PendingStatus>,
}

#[derive(Debug, Default)]
pub struct StatusPoll {
    pub changed: bool,
    pub newly_disabled: Option<String>,
}

impl StatusRuntime {
    pub fn new(specs: Vec<StatusSpec>) -> Self {
        let now = Instant::now();
        Self { entries: specs.into_iter().map(|spec| StatusEntry {
            spec, value: None, sha256: None, inode: None, valid_until: None, disabled: false,
            refreshes: 0, failures: 0, consecutive_failures: 0, bytes_read: 0,
            elapsed_ms: 0, last_elapsed_ms: None, last_error: None, expirations: 0, next_due: now,
        }).collect(), pending: None }
    }

    /// One worker at a time. A slow owner file consumes at most three timed
    /// attempts before its status is disabled for this TUI process.
    pub fn poll(&mut self, now: Instant) -> StatusPoll {
        let mut outcome = StatusPoll::default();
        for entry in &mut self.entries {
            if entry.value.is_some() && entry.value_at(now).is_none() {
                entry.value = None;
                entry.sha256 = None;
                entry.inode = None;
                entry.valid_until = None;
                entry.last_error = Some("stale_cached_value");
                entry.expirations = entry.expirations.saturating_add(1);
                outcome.changed = true;
            }
        }
        if let Some(pending) = self.pending.take() {
            let elapsed = now.saturating_duration_since(pending.started);
            let result = match pending.receiver.try_recv() {
                Ok(completion) if completion.elapsed < STATUS_TIMEOUT =>
                    Some((completion.result, completion.elapsed, completion.observed_bytes)),
                Ok(completion) => Some((Err("refresh_timeout"), completion.elapsed, completion.observed_bytes)),
                Err(TryRecvError::Disconnected) => Some((Err("worker_stopped"), elapsed, 0)),
                Err(TryRecvError::Empty) if elapsed >= STATUS_TIMEOUT => Some((Err("refresh_timeout"), elapsed, 0)),
                Err(TryRecvError::Empty) => None,
            };
            if let Some((result, measured, observed_bytes)) = result {
                let entry = &mut self.entries[pending.index];
                if entry.record(result, now, measured, observed_bytes) {
                    outcome.newly_disabled = Some(entry.spec.plugin.clone());
                }
                outcome.changed = true;
            } else {
                self.pending = Some(pending);
                return outcome;
            }
        }
        if let Some(index) = self.entries.iter().position(|entry| !entry.disabled && now >= entry.next_due) {
            let spec = self.entries[index].spec.clone();
            let (sender, receiver) = mpsc::sync_channel(1);
            std::thread::spawn(move || {
                let started = Instant::now();
                let mut observed_bytes = 0;
                let result = read_status(&spec, &mut observed_bytes).map_err(|error| failure_code(&error));
                let _ = sender.send(StatusCompletion { result, elapsed: started.elapsed(), observed_bytes });
            });
            self.pending = Some(PendingStatus { index, started: now, receiver });
        }
        outcome
    }

    pub fn chip_label(&self) -> Option<String> {
        if self.entries.is_empty() { return None; }
        let now = Instant::now();
        let disabled = self.entries.iter().filter(|entry| entry.disabled).count();
        if disabled > 0 { return Some(format!("Plugin status disabled ({disabled})")); }
        if self.entries.len() == 1 {
            let entry = &self.entries[0];
            return Some(format!("{}: {}", entry.spec.label, entry.value_at(now).unwrap_or("?")));
        }
        let ready = self.entries.iter().filter(|entry| entry.value_at(now).is_some()).count();
        Some(format!("Plugin status {ready}/{}", self.entries.len()))
    }

    pub fn ledger_lines(&self) -> Vec<String> {
        let now = Instant::now();
        self.entries.iter().flat_map(|entry| {
            let value = entry.value_at(now);
            let state = if entry.disabled { "disabled" } else if value.is_some() { "current" } else { "unknown" };
            let mut lines = vec![format!("{} · {} · {}", entry.spec.plugin, entry.spec.label, state),
                format!("  value: {}", value.unwrap_or("?")),
                "  producer: owner-file-v1 (DOXA executes no plugin code)".into(),
                format!("  source: {}", crate::ui::safe_label(&entry.spec.source.display().to_string())),
                format!("  refresh: every {} s · max {} bytes · timeout {} ms · disable after {} failures",
                    entry.spec.refresh_seconds, MAX_STATUS_FILE, STATUS_TIMEOUT.as_millis(), STATUS_FAILURE_BUDGET),
                format!("  ledger: {} attempts · {} failures ({} consecutive) · {} expirations · {} observed bytes · {} ms total · last {} ms",
                    entry.refreshes, entry.failures, entry.consecutive_failures, entry.expirations, entry.bytes_read,
                    entry.elapsed_ms, entry.last_elapsed_ms.map_or("?".into(), |ms| ms.to_string())),
            ];
            if let Some(error) = entry.last_error { lines.push(format!("  last error: {error}")); }
            if value.is_some() {
                if let Some(sha) = &entry.sha256 { lines.push(format!("  SHA-256: {sha}")); }
                if let Some((device, inode)) = entry.inode { lines.push(format!("  inode: {device}:{inode}")); }
            }
            lines.push(String::new());
            lines
        }).collect()
    }
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

    const STATUS_MANIFEST: &str = "api_version = 1\nname = 'demo'\nversion = '1.0'\n[status]\nproducer = 'owner-file-v1'\nlabel = 'Queue'\nrefresh_seconds = 5\n";

    #[test]
    fn allowlisted_status_only_manifest_reads_one_private_owner_value_without_code() {
        let dir = fixture();
        write(&dir.path().join("config.toml"), "native_plugins = ['demo']\n");
        write(&dir.path().join("native-plugins/demo.toml"), STATUS_MANIFEST);
        write(&dir.path().join("native-plugins/demo.status.toml"), "value = 'Ready'\n");
        write(&dir.path().join("native-plugins/hidden.toml"), &STATUS_MANIFEST.replace("demo", "hidden"));
        let inventory = load(dir.path(), &[]).unwrap();
        assert!(inventory.commands.is_empty() && inventory.failures.is_empty());
        assert_eq!(inventory.statuses.len(), 1);
        let mut observed_bytes = 0;
        let read = read_status(&inventory.statuses[0], &mut observed_bytes).unwrap();
        assert_eq!(read.value, "Ready");
        assert_eq!(observed_bytes, "value = 'Ready'\n".len() as u64);
        assert_eq!(read.sha256.len(), 64);
    }

    #[test]
    fn status_file_and_manifest_boundaries_fail_closed() {
        let dir = fixture();
        write(&dir.path().join("config.toml"), "native_plugins = ['demo']\n");
        let manifest = dir.path().join("native-plugins/demo.toml");
        let status = dir.path().join("native-plugins/demo.status.toml");
        write(&manifest, STATUS_MANIFEST);
        let spec = load(dir.path(), &[]).unwrap().statuses.remove(0);
        write(&status, "value = 'Ready'\nexec = '/bin/sh'\n");
        let mut observed_bytes = 0;
        assert!(read_status(&spec, &mut observed_bytes).is_err());
        assert!(observed_bytes > 0, "invalid content still costs read bytes");
        write(&status, "value = 'Ready'\n");
        std::fs::set_permissions(&status, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(read_status(&spec, &mut observed_bytes).is_err());
        std::fs::remove_file(&status).unwrap();
        symlink(dir.path().join("outside.toml"), &status).unwrap();
        assert!(read_status(&spec, &mut observed_bytes).is_err());
        std::fs::remove_file(&status).unwrap();
        write(&status, &format!("value = '{}'\n", "x".repeat(1024)));
        assert!(read_status(&spec, &mut observed_bytes).is_err());
        write(&status, "value = 'Old'\n");
        let old = SystemTime::now() - Duration::from_secs(16);
        File::options().write(true).open(&status).unwrap()
            .set_times(std::fs::FileTimes::new().set_modified(old)).unwrap();
        let error = read_status(&spec, &mut observed_bytes).unwrap_err();
        assert_eq!(failure_code(&error), "stale_file");
        assert!(observed_bytes > 0);
        write(&status, "value = 'Future'\n");
        let future = SystemTime::now() + Duration::from_secs(30);
        File::options().write(true).open(&status).unwrap()
            .set_times(std::fs::FileTimes::new().set_modified(future)).unwrap();
        assert_eq!(failure_code(&read_status(&spec, &mut observed_bytes).unwrap_err()), "stale_file");
        write(&manifest, &STATUS_MANIFEST.replace("owner-file-v1", "exec-v1"));
        assert!(load(dir.path(), &[]).unwrap().statuses.is_empty());
        assert_eq!(load(dir.path(), &[]).unwrap().failures.len(), 1);
        write(&manifest, &STATUS_MANIFEST.replace("refresh_seconds = 5", "refresh_seconds = 1"));
        assert!(load(dir.path(), &[]).unwrap().statuses.is_empty());
        write(&manifest, &format!("{STATUS_MANIFEST}exec = '/bin/sh'\n"));
        assert!(load(dir.path(), &[]).unwrap().statuses.is_empty());
    }

    #[test]
    fn slow_or_failing_status_disables_after_bounded_cost_and_exposes_ledger() {
        let dir = fixture();
        write(&dir.path().join("config.toml"), "native_plugins = ['demo']\n");
        write(&dir.path().join("native-plugins/demo.toml"), STATUS_MANIFEST);
        let spec = load(dir.path(), &[]).unwrap().statuses.remove(0);
        let mut runtime = StatusRuntime::new(vec![spec]);
        let start = Instant::now();
        for attempt in 0..3 {
            let now = start + Duration::from_secs(attempt * 5);
            let (_sender, receiver) = mpsc::sync_channel(1);
            runtime.pending = Some(PendingStatus { index: 0, started: now, receiver });
            let result = runtime.poll(now + STATUS_TIMEOUT + Duration::from_millis(1));
            assert!(result.changed);
            assert_eq!(result.newly_disabled.is_some(), attempt == 2);
        }
        assert_eq!(runtime.chip_label().as_deref(), Some("Plugin status disabled (1)"));
        assert_eq!(runtime.entries[0].refreshes, 3);
        assert_eq!(runtime.entries[0].failures, 3);
        assert_eq!(runtime.entries[0].bytes_read, 0);
        assert!(runtime.ledger_lines().join("\n").contains("last error: refresh_timeout"));
        assert!(runtime.pending.is_none());
        assert!(!runtime.poll(start + Duration::from_secs(100)).changed);
        assert!(runtime.pending.is_none(), "disabled status cannot start a fourth worker");
    }

    #[test]
    fn successful_refresh_resets_failure_streak_and_records_actual_read_cost() {
        let dir = fixture();
        write(&dir.path().join("config.toml"), "native_plugins = ['demo']\n");
        write(&dir.path().join("native-plugins/demo.toml"), STATUS_MANIFEST);
        let spec = load(dir.path(), &[]).unwrap().statuses.remove(0);
        let mut runtime = StatusRuntime::new(vec![spec]);
        let now = Instant::now();
        runtime.entries[0].record(Err("missing_file"), now, Duration::from_millis(4), 0);
        runtime.entries[0].record(Ok(StatusRead { value: "Ready".into(), sha256: "a".repeat(64),
            inode: 2, device: 1, valid_until: now + Duration::from_secs(15) }), now, Duration::from_millis(8), 18);
        assert_eq!(runtime.entries[0].consecutive_failures, 0);
        assert_eq!(runtime.chip_label().as_deref(), Some("Queue: Ready"));
        assert_eq!(runtime.entries[0].bytes_read, 18);
        assert_eq!(runtime.entries[0].elapsed_ms, 12);
        assert!(runtime.ledger_lines().join("\n").contains("2 attempts · 1 failures"));
    }

    #[test]
    fn cached_value_expires_without_a_read_or_inaccurate_refresh_count() {
        let dir = fixture();
        write(&dir.path().join("config.toml"), "native_plugins = ['demo']\n");
        write(&dir.path().join("native-plugins/demo.toml"), STATUS_MANIFEST);
        let spec = load(dir.path(), &[]).unwrap().statuses.remove(0);
        let mut runtime = StatusRuntime::new(vec![spec]);
        let now = Instant::now();
        runtime.entries[0].record(Ok(StatusRead { value: "Ready".into(), sha256: "a".repeat(64),
            inode: 2, device: 1, valid_until: now - Duration::from_secs(1) }),
            now - Duration::from_secs(16), Duration::from_millis(8), 18);
        assert_eq!(runtime.chip_label().as_deref(), Some("Queue: ?"));
        // A pending worker prevents a new read while the expired value is cleared.
        let (_sender, receiver) = mpsc::sync_channel(1);
        runtime.pending = Some(PendingStatus { index: 0, started: now, receiver });
        assert!(runtime.poll(now).changed);
        assert_eq!(runtime.entries[0].refreshes, 1);
        assert_eq!(runtime.entries[0].expirations, 1);
        assert!(runtime.ledger_lines().join("\n").contains("stale_cached_value"));
    }

    #[test]
    fn delayed_worker_completion_cannot_revive_an_expired_value() {
        let dir = fixture();
        write(&dir.path().join("config.toml"), "native_plugins = ['demo']\n");
        write(&dir.path().join("native-plugins/demo.toml"), STATUS_MANIFEST);
        let spec = load(dir.path(), &[]).unwrap().statuses.remove(0);
        let mut runtime = StatusRuntime::new(vec![spec]);
        let now = Instant::now();
        runtime.entries[0].record(Ok(StatusRead { value: "Old".into(), sha256: "a".repeat(64),
            inode: 2, device: 1, valid_until: now - Duration::from_millis(1) }),
            now, Duration::from_millis(10), 14);
        assert_eq!(runtime.chip_label().as_deref(), Some("Queue: ?"));
        assert_eq!(runtime.entries[0].failures, 1);
        assert_eq!(runtime.entries[0].last_error, Some("stale_completion"));
        assert_eq!(runtime.entries[0].bytes_read, 14);
    }
}

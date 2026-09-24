// SPDX-License-Identifier: AGPL-3.0-only
//! Python-compatible, local peer presence registry and bounded local messaging.
pub mod delivery;
use serde::{Deserialize, Serialize};
use std::ffi::OsStr;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{FileTypeExt, OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;
use time::{format_description::well_known::Iso8601, OffsetDateTime};

pub const STALE_AFTER_SECS: i64 = 60;
pub const HEARTBEAT_SECS: u64 = 15;
pub const MAX_ENTRY_BYTES: u64 = 64 * 1024;
pub const MAX_REGISTRY_ENTRIES: usize = 4096;
pub const MAX_SELF_DESC_CHARS: usize = 64;
static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Consumer supplied LORE scrub adapter. There is deliberately no identity/default scrubber.
pub trait Scrubber {
    fn scrub(&self, text: &str) -> String;
}
impl<F: Fn(&str) -> String> Scrubber for F {
    fn scrub(&self, text: &str) -> String { self(text) }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct PeerRecord {
    pub session_id: String,
    pub pid: i32,
    pub socket_path: String,
    pub cwd: String,
    pub repo_root: Option<String>,
    pub title: String,
    pub started_at: String,
    pub heartbeat_at: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub daemon_socket: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub clients: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub usage_tokens: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub provider: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub engine: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub parent_session_id: Option<String>,
}
impl PeerRecord {
    pub fn scope_key(&self) -> &str { self.repo_root.as_deref().unwrap_or(&self.cwd) }
    fn scrub_display(&mut self, scrubber: &impl Scrubber) {
        self.title = scrubber.scrub(&self.title);
        self.cwd = scrubber.scrub(&self.cwd);
        for value in [&mut self.provider, &mut self.model, &mut self.engine] {
            *value = value.take().and_then(|s| {
                let s = scrubber.scrub(&s);
                let s = s.trim();
                if s.is_empty() { return None; }
                if s.chars().count() > MAX_SELF_DESC_CHARS {
                    Some(s.chars().take(MAX_SELF_DESC_CHARS - 1).collect::<String>() + "…")
                } else { Some(s.to_owned()) }
            });
        }
        self.parent_session_id = self.parent_session_id.take().map(|s| scrubber.scrub(&s));
    }
}

/// Runtime override follows Python's DOXA_RUNTIME_DIR / XDG_RUNTIME_DIR precedence.
pub fn runtime_dir() -> PathBuf {
    if let Some(p) = std::env::var_os("DOXA_RUNTIME_DIR").filter(|s| !s.is_empty()) { return p.into(); }
    if let Some(p) = std::env::var_os("XDG_RUNTIME_DIR").filter(|s| !s.is_empty()) { return PathBuf::from(p).join("doxa"); }
    PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".local/share/doxa")
}

/// Main worktree checkout is the shared scope; outside git, use the cwd.
pub fn scope_for_cwd(cwd: &Path) -> io::Result<String> {
    let output = std::process::Command::new("git")
        .args(["rev-parse", "--path-format=absolute", "--git-common-dir"])
        .current_dir(cwd).output();
    if let Ok(out) = output {
        if out.status.success() {
            let common = String::from_utf8_lossy(&out.stdout).trim().to_owned();
            if !common.is_empty() {
                let p = PathBuf::from(common);
                return Ok(if p.file_name() == Some(OsStr::new(".git")) {
                    p.parent().unwrap_or(&p).to_string_lossy().into_owned()
                } else { p.to_string_lossy().into_owned() });
            }
        }
    }
    Ok(cwd.canonicalize()?.to_string_lossy().into_owned())
}

fn owner_private_dir(path: &Path) -> io::Result<()> {
    if path.exists() {
        let meta = fs::symlink_metadata(path)?;
        if !meta.file_type().is_dir() { return Err(io::Error::new(io::ErrorKind::InvalidData, "runtime component is not a directory")); }
        if meta.uid() != unsafe { libc::geteuid() } { return Err(io::Error::new(io::ErrorKind::PermissionDenied, "runtime directory has another owner")); }
    } else { fs::create_dir_all(path)?; }
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
}
use std::os::unix::fs::MetadataExt;

pub struct Registry { runtime: PathBuf, directory: PathBuf }
impl Registry {
    pub fn open(runtime: impl AsRef<Path>) -> io::Result<Self> {
        let runtime = runtime.as_ref().to_path_buf();
        owner_private_dir(&runtime)?;
        let directory = runtime.join("registry");
        owner_private_dir(&directory)?;
        Ok(Self { runtime, directory })
    }
    pub fn directory(&self) -> &Path { &self.directory }
    pub fn runtime(&self) -> &Path { &self.runtime }

    pub fn write(&self, record: &PeerRecord) -> io::Result<()> {
        if !safe_id(&record.session_id) || record.pid <= 0 { return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid peer identity")); }
        let dest = self.directory.join(format!("{}.json", record.session_id));
        let bytes = serde_json::to_vec(record).map_err(io::Error::other)?;
        if bytes.len() as u64 > MAX_ENTRY_BYTES { return Err(io::Error::new(io::ErrorKind::InvalidInput, "registry entry too large")); }
        let n = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        let tmp = self.directory.join(format!(".{}-{}-{n}.tmp", record.session_id, std::process::id()));
        let result = (|| {
            let mut file = OpenOptions::new().write(true).create_new(true).mode(0o600).open(&tmp)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            fs::rename(&tmp, &dest)?;
            File::open(&self.directory)?.sync_all()?;
            Ok(())
        })();
        if result.is_err() { let _ = fs::remove_file(&tmp); }
        result
    }
    pub fn heartbeat(&self, record: &mut PeerRecord) -> io::Result<()> {
        record.heartbeat_at = now();
        self.write(record)
    }
    pub fn read(&self, scrubber: &impl Scrubber, reap: bool, probe: bool) -> io::Result<Vec<PeerRecord>> {
        let mut paths: Vec<_> = fs::read_dir(&self.directory)?.take(MAX_REGISTRY_ENTRIES).filter_map(Result::ok).map(|e| e.path())
            .filter(|p| p.extension() == Some(OsStr::new("json"))).collect();
        paths.sort();
        let mut out = Vec::new();
        for path in paths {
            let record = read_one(&path);
            match record {
                Ok(mut peer) => {
                    if path.file_stem() != Some(OsStr::new(&peer.session_id)) || !safe_id(&peer.session_id) {
                        if reap { remove_regular_entry(&path); }
                        continue;
                    }
                    let alive = pid_alive(peer.pid);
                    if !alive || stale(&peer.heartbeat_at) {
                        if reap { remove_regular_entry(&path); self.reap_socket(&peer.socket_path, !alive); }
                        continue;
                    }
                    if probe && std::os::unix::net::UnixStream::connect(&peer.socket_path).is_err() { continue; }
                    peer.scrub_display(scrubber);
                    out.push(peer);
                }
                Err(_) => { if reap { remove_regular_entry(&path); } }
            }
        }
        Ok(out)
    }
    pub fn scoped(&self, scope: &str, self_id: Option<&str>, scrubber: &impl Scrubber, probe: bool) -> io::Result<Vec<PeerRecord>> {
        let scrubbed_scope = scrubber.scrub(scope);
        Ok(self.read(scrubber, true, probe)?.into_iter()
            .filter(|p| (p.scope_key() == scope || p.scope_key() == scrubbed_scope) && Some(p.session_id.as_str()) != self_id).collect())
    }
    pub fn sweep_stale(&self, scrubber: &impl Scrubber) -> io::Result<usize> {
        let before = fs::read_dir(&self.directory)?.filter_map(Result::ok).filter(|e| e.path().extension() == Some(OsStr::new("json"))).count();
        // A failed socket probe alone does not prove that a live peer has
        // exited. Its listener may be temporarily unavailable; let its PID
        // and heartbeat determine whether its presence can be reaped.
        let _ = self.read(scrubber, true, false)?;
        let after = fs::read_dir(&self.directory)?.filter_map(Result::ok).filter(|e| e.path().extension() == Some(OsStr::new("json"))).count();
        Ok(before.saturating_sub(after))
    }
    fn reap_socket(&self, name: &str, pid_dead: bool) {
        if !pid_dead { return; }
        let path = Path::new(name);
        // Reject traversal and symlinks before canonicalizing. An untrusted record never chooses an arbitrary unlink target.
        if !path.is_absolute() || path.components().any(|c| !matches!(c, Component::RootDir | Component::Normal(_))) { return; }
        let Ok(meta) = fs::symlink_metadata(path) else { return; };
        if !meta.file_type().is_socket() || meta.uid() != unsafe { libc::geteuid() } { return; }
        let Ok(base) = self.runtime.canonicalize() else { return; };
        let Ok(target) = path.canonicalize() else { return; };
        if target == base || !target.starts_with(&base) { return; }
        if std::os::unix::net::UnixStream::connect(path).is_ok() { return; }
        let _ = fs::remove_file(path);
    }
}
fn safe_id(s: &str) -> bool {
    !s.is_empty() && s.len() <= 128 && s.bytes().enumerate().all(|(i, b)|
        b.is_ascii_alphanumeric() || (i > 0 && b == b'-'))
}
fn remove_regular_entry(path: &Path) {
    if fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_file() && m.uid() == unsafe { libc::geteuid() }) {
        let _ = fs::remove_file(path);
    }
}
fn read_one(path: &Path) -> io::Result<PeerRecord> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.file_type().is_file() || meta.len() > MAX_ENTRY_BYTES || meta.uid() != unsafe { libc::geteuid() } { return Err(io::Error::new(io::ErrorKind::InvalidData, "unsafe registry entry")); }
    // Recheck the opened inode: an entry can be swapped for a FIFO or hard
    // link after symlink_metadata but before open. O_NONBLOCK avoids hanging.
    let mut file = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path)?;
    let opened = file.metadata()?;
    if !opened.file_type().is_file() || opened.uid() != unsafe { libc::geteuid() }
        || opened.nlink() != 1 || opened.len() > MAX_ENTRY_BYTES {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "unsafe registry entry"));
    }
    let mut bytes = Vec::new();
    Read::by_ref(&mut file).take(MAX_ENTRY_BYTES + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_ENTRY_BYTES { return Err(io::Error::new(io::ErrorKind::InvalidData, "oversized registry entry")); }
    serde_json::from_slice(&bytes).map_err(io::Error::other)
}
fn pid_alive(pid: i32) -> bool {
    if pid <= 0 { return false; }
    let status = unsafe { libc::kill(pid, 0) };
    status == 0 || io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}
fn stale(s: &str) -> bool {
    let Ok(t) = OffsetDateTime::parse(s, &Iso8601::DEFAULT) else { return true; };
    (OffsetDateTime::now_utc() - t).whole_seconds() > STALE_AFTER_SECS
}
pub fn now() -> String {
    let t = OffsetDateTime::now_utc();
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:06}Z", t.year(), t.month() as u8, t.day(), t.hour(), t.minute(), t.second(), t.microsecond())
}
pub fn heartbeat_interval() -> Duration { Duration::from_secs(HEARTBEAT_SECS) }

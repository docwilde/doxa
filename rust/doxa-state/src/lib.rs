// SPDX-License-Identifier: AGPL-3.0-only
//! Local state shared with DOXA 1.x. No daemon or UI lifecycle is owned here.

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Read;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};

pub const MAX_CONFIG_BYTES: u64 = 1024 * 1024;
pub const MAX_TABSET_BYTES: u64 = 8 * 1024 * 1024;
pub const MAX_MACHINE_ID_BYTES: u64 = 256;

fn read_bounded_regular(path: &Path, limit: u64) -> io::Result<Vec<u8>> {
    // Recheck the opened inode so a file swapped for a FIFO between path
    // inspection and open cannot block startup or bypass the size limit.
    let file = fs::OpenOptions::new().read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file() || meta.nlink() != 1 || meta.len() > limit {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "unsafe or oversized state file"));
    }
    let mut raw = Vec::new();
    file.take(limit + 1).read_to_end(&mut raw)?;
    if raw.len() as u64 > limit {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "oversized state file"));
    }
    Ok(raw)
}

/// The ASCII filename grammar in `doxa.identity.valid_session_id`.
pub fn valid_session_id(id: &str) -> bool {
    let bytes = id.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 128
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || *b == b'-')
}

pub fn require_session_id(id: &str) -> Result<&str, &'static str> {
    if valid_session_id(id) { Ok(id) } else { Err("invalid session id: expected letters, digits and dashes only") }
}

/// Match `DOXA_RUNTIME_DIR`, then `XDG_RUNTIME_DIR/doxa`, then the home fallback.
pub fn runtime_dir(home: &Path, doxa_runtime_dir: Option<&str>, xdg_runtime_dir: Option<&str>) -> PathBuf {
    if let Some(value) = doxa_runtime_dir.filter(|s| !s.trim().is_empty()) {
        return PathBuf::from(value.trim());
    }
    if let Some(value) = xdg_runtime_dir.filter(|s| !s.trim().is_empty()) {
        return PathBuf::from(value.trim()).join("doxa");
    }
    home.join(".local/share/doxa")
}

/// Upper bound for one advisory presence file. The Python writer emits only a few KB.
pub const MAX_REGISTRY_BYTES: u64 = 64 * 1024;

/// Raw routing fields are kept separate from display text. Never render these
/// fields directly; paths and IDs are used only to locate the daemon.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DaemonRoute {
    pub session_id: String,
    pub pid: i32,
    pub socket_path: String,
    pub daemon_socket: String,
    pub scope_key: String,
    pub started_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DaemonDisplay {
    pub title: String,
    pub cwd: String,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub engine: Option<String>,
    pub parent_session_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DaemonInfo {
    pub route: DaemonRoute,
    pub display: DaemonDisplay,
}

/// Read valid live daemon entries, in newest-first order, without modifying registry files.
/// The required scrubber runs over each exposed display string. Unknown fields
/// are dropped; raw routing strings are separate and must never be rendered.
pub fn list_daemons(registry_dir: &Path, scope: Option<&str>, scrub: impl Fn(&str) -> String) -> Vec<DaemonInfo> {
    let mut peers = Vec::new();
    let Ok(paths) = fs::read_dir(registry_dir) else { return peers };
    for entry in paths.flatten() {
        if entry.path().extension().and_then(|s| s.to_str()) != Some("json") { continue; }
        let Ok(raw) = read_bounded_regular(&entry.path(), MAX_REGISTRY_BYTES) else { continue; };
        let Ok(value) = serde_json::from_slice::<Value>(&raw) else { continue; };
        let Some(map) = value.as_object() else { continue; };
        if !["session_id", "pid", "socket_path", "cwd", "repo_root", "title", "started_at", "heartbeat_at"]
            .iter().all(|key| map.contains_key(*key)) { continue; }
        let Some(session_id) = map.get("session_id").and_then(Value::as_str).filter(|s| valid_session_id(s)) else { continue; };
        let Some(daemon_socket) = map.get("daemon_socket").and_then(Value::as_str).filter(|s| !s.is_empty()) else { continue; };
        let Some(socket_path) = map.get("socket_path").and_then(Value::as_str) else { continue; };
        let Some(cwd) = map.get("cwd").and_then(Value::as_str) else { continue; };
        let Some(title) = map.get("title").and_then(Value::as_str) else { continue; };
        let Some(started_at) = map.get("started_at").and_then(Value::as_str) else { continue; };
        let Some(pid) = map.get("pid").and_then(Value::as_i64) else { continue; };
        if pid <= 0 || pid > i32::MAX as i64 { continue; }
        // kill(pid, 0) reports EPERM for an existing process owned by another user.
        if unsafe { libc::kill(pid as i32, 0) } != 0
            && io::Error::last_os_error().raw_os_error() != Some(libc::EPERM) { continue; }
        let Some(heartbeat) = map.get("heartbeat_at").and_then(Value::as_str) else { continue; };
        let format = time::macros::format_description!("[year]-[month]-[day]T[hour]:[minute]:[second].[subsecond]Z");
        let Ok(ts) = time::PrimitiveDateTime::parse(heartbeat, &format) else { continue; };
        let age = time::OffsetDateTime::now_utc().unix_timestamp() - ts.assume_utc().unix_timestamp();
        if age > 60 { continue; }
        let scope_key = map.get("repo_root").and_then(Value::as_str).filter(|s| !s.is_empty()).unwrap_or(cwd);
        if scope.is_some() && scope != Some(scope_key) { continue; }
        let display_optional = |name: &str| map.get(name).and_then(Value::as_str).filter(|s| !s.is_empty()).map(&scrub);
        peers.push(DaemonInfo {
            route: DaemonRoute {
                session_id: session_id.into(), pid: pid as i32,
                socket_path: socket_path.into(), daemon_socket: daemon_socket.into(),
                scope_key: scope_key.into(), started_at: started_at.into(),
            },
            display: DaemonDisplay {
                title: scrub(title), cwd: scrub(cwd),
                provider: display_optional("provider"), model: display_optional("model"),
                engine: display_optional("engine"), parent_session_id: display_optional("parent_session_id"),
            },
        });
    }
    peers.sort_by(|a, b| b.route.started_at.cmp(&a.route.started_at));
    peers
}

/// Python's config load failure policy: malformed or absent means empty.
pub fn load_config(path: &Path) -> toml::Table {
    read_bounded_regular(path, MAX_CONFIG_BYTES).ok()
        .and_then(|raw| String::from_utf8(raw).ok())
        .and_then(|s| s.parse::<toml::Table>().ok()).unwrap_or_default()
}

/// `doxa.config.raw` precedence for a key supplied by the caller's settings table.
pub fn raw_setting(env_value: Option<&str>, stored: &toml::Table, key: &str) -> String {
    if let Some(value) = env_value.filter(|s| !s.trim().is_empty()) { return value.to_string(); }
    match stored.get(key) {
        Some(toml::Value::Boolean(true)) => "1".into(),
        Some(toml::Value::Boolean(false)) | None => String::new(),
        Some(toml::Value::String(s)) => s.clone(),
        Some(value) => value.to_string(),
    }
}

/// Preserve all TOML values (including dates) while replacing the file securely.
pub fn save_config(path: &Path, stored: &toml::Table) -> io::Result<()> {
    let body = toml::to_string_pretty(stored).map_err(io::Error::other)?;
    atomic_write(path, body.as_bytes())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Tab {
    pub session_id: String,
    pub pinned_name: Option<String>,
    pub cwd: Option<String>,
}

/// Original JSON is retained so a save leaves layout, collections and future keys intact.
#[derive(Debug, Clone)]
pub struct TabSet {
    pub scope_key: String,
    pub active_session_id: Option<String>,
    pub tabs: Vec<Tab>,
    pub raw: Map<String, Value>,
}

fn digest(text: &str, hex_chars: usize) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))[..hex_chars].to_string()
}

pub fn tabset_path(home: &Path, scope_key: &str, machine_id: &str) -> PathBuf {
    home.join("tabsets").join(format!("{}-{}.json", digest(scope_key, 24), digest(machine_id, 12)))
}

/// Read an existing machine id. Read-only paths never mint one.
pub fn machine_id(home: &Path) -> io::Result<String> {
    let id = String::from_utf8(read_bounded_regular(&home.join("machine-id"), MAX_MACHINE_ID_BYTES)?)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid machine id encoding"))?;
    let id = id.trim();
    if id.is_empty() { return Err(io::Error::new(io::ErrorKind::InvalidData, "empty machine id")); }
    Ok(id.into())
}

pub fn load_tabset(path: &Path, fallback_scope: &str) -> Option<TabSet> {
    if fallback_scope.is_empty() { return None; }
    let value: Value = serde_json::from_slice(&read_bounded_regular(path, MAX_TABSET_BYTES).ok()?).ok()?;
    let data = value.as_object()?.clone();
    let rows = data.get("tabs").and_then(Value::as_array).or_else(|| {
        let layout = data.get("layout")?.as_object()?;
        (layout.get("kind")?.as_str()? == "tabs").then(|| layout.get("tabs")?.as_array()).flatten()
    })?;
    let mut tabs = Vec::new();
    for row in rows {
        let Some(obj) = row.as_object() else { continue; };
        let Some(id) = obj.get("session_id").and_then(Value::as_str).map(str::trim) else { continue; };
        if !valid_session_id(id) { continue; }
        tabs.push(Tab {
            session_id: id.into(),
            pinned_name: obj.get("pinned_name").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_owned),
            cwd: obj.get("cwd").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_owned),
        });
    }
    if tabs.is_empty() { return None; }
    Some(TabSet {
        scope_key: data.get("scope_key").and_then(Value::as_str).filter(|s| !s.is_empty()).unwrap_or(fallback_scope).into(),
        active_session_id: data.get("active_session_id").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_owned),
        tabs,
        raw: data,
    })
}

pub fn save_tabset(path: &Path, record: &TabSet) -> io::Result<()> {
    if record.scope_key.is_empty() { return Err(io::Error::new(io::ErrorKind::InvalidInput, "empty scope key")); }
    if record.tabs.iter().any(|t| !valid_session_id(&t.session_id)) {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid session id"));
    }
    // Until layout and collection pruning is ported, changing membership would
    // leave stale session references inside their Python-owned structures.
    let has_structure = record.raw.get("layout").and_then(Value::as_object)
        .is_some_and(|layout| layout.contains_key("trees") || layout.contains_key("groups"))
        || record.raw.contains_key("collections");
    if has_structure {
        let old_rows = record.raw.get("tabs").and_then(Value::as_array).or_else(|| {
            let layout = record.raw.get("layout")?.as_object()?;
            (layout.get("kind")?.as_str()? == "tabs").then(|| layout.get("tabs")?.as_array()).flatten()
        });
        let old: Vec<&str> = old_rows
            .into_iter().flatten()
            .filter_map(|row| row.get("session_id").and_then(Value::as_str))
            .filter(|id| valid_session_id(id))
            .collect();
        let new: Vec<&str> = record.tabs.iter().map(|tab| tab.session_id.as_str()).collect();
        if old != new {
            return Err(io::Error::new(io::ErrorKind::InvalidInput,
                "cannot change tab IDs while layout or collections require pruning"));
        }
    }
    let mut data = record.raw.clone();
    let rows: Vec<Value> = record.tabs.iter().map(|t| serde_json::json!({"session_id":t.session_id,"pinned_name":t.pinned_name,"cwd":t.cwd})).collect();
    data.insert("scope_key".into(), Value::String(record.scope_key.clone()));
    data.insert("active_session_id".into(), record.active_session_id.clone().map(Value::String).unwrap_or(Value::Null));
    data.insert("tabs".into(), Value::Array(rows.clone()));
    let layout = data.entry("layout").or_insert_with(|| Value::Object(Map::new()));
    if let Some(layout) = layout.as_object_mut() {
        layout.insert("kind".into(), Value::String("tabs".into()));
        layout.insert("tabs".into(), Value::Array(rows));
    }
    atomic_write(path, &serde_json::to_vec(&data).map_err(io::Error::other)?)
}

fn atomic_write(path: &Path, bytes: &[u8]) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let parent = path.parent().ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "no parent directory"))?;
    let created = match fs::symlink_metadata(parent) {
        Ok(_) => false,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            fs::create_dir_all(parent)?;
            true
        }
        Err(error) => return Err(error),
    };
    let dir = fs::OpenOptions::new().read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW).open(parent)?;
    if created { dir.set_permissions(fs::Permissions::from_mode(0o700))?; }
    let meta = dir.metadata()?;
    if !meta.is_dir() || meta.uid() != unsafe { libc::geteuid() }
        || meta.permissions().mode() & 0o022 != 0 {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "state directory must be owned and not writable by others"));
    }
    let mut tmp = tempfile::Builder::new().prefix(".doxa-state-").tempfile_in(parent)?;
    tmp.as_file().set_permissions(fs::Permissions::from_mode(0o600))?;
    tmp.write_all(bytes)?;
    tmp.as_file().sync_all()?;
    tmp.persist(path).map_err(|error| error.error)?;
    dir.sync_all()?;
    Ok(())
}

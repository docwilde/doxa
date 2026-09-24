//! Read-only discovery of attachable Python daemon sessions.
//!
//! The peer registry is a hint, not authority. Its files are never reaped by
//! this frontend, and every path taken from an entry is checked before use.

use std::env;
use std::fs::{self, File};
use std::io::{self, Read};
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};

use serde::Deserialize;
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;

const STALE_SECS: i64 = 60;
const MAX_ENTRY_BYTES: u64 = 64 * 1024;

#[derive(Debug, Clone)]
pub struct Session {
    pub id: String,
    pub socket: PathBuf,
    pub clients: Option<u64>,
    pub started_at: String,
}

#[derive(Deserialize)]
struct Entry {
    session_id: String,
    pid: i32,
    heartbeat_at: String,
    started_at: String,
    #[serde(rename = "title")]
    _title: String,
    daemon_socket: Option<String>,
    clients: Option<u64>,
}

/// Match Python's `peers.runtime_dir` without creating or changing a directory.
pub fn runtime_dir() -> io::Result<PathBuf> {
    if let Some(path) = nonempty_env("DOXA_RUNTIME_DIR") {
        return Ok(PathBuf::from(path));
    }
    if let Some(path) = nonempty_env("XDG_RUNTIME_DIR") {
        return Ok(PathBuf::from(path).join("doxa"));
    }
    let home = nonempty_env("HOME").ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::NotFound,
            "HOME is unset; cannot find DOXA registry",
        )
    })?;
    Ok(PathBuf::from(home).join(".local/share/doxa"))
}

fn nonempty_env(name: &str) -> Option<String> {
    env::var(name)
        .ok()
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty())
}

fn trusted_dir(path: &Path, uid: u32) -> io::Result<()> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.file_type().is_dir() || meta.uid() != uid || meta.permissions().mode() & 0o077 != 0 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            format!("insecure DOXA directory: {}", path.display()),
        ));
    }
    Ok(())
}

/// List live, attachable daemon sessions across all project scopes.
pub fn sessions() -> io::Result<Vec<Session>> {
    let runtime = runtime_dir()?;
    let uid = unsafe { libc::geteuid() };
    match trusted_dir(&runtime, uid) {
        Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(err) => return Err(err),
        Ok(()) => {}
    }
    let registry = runtime.join("registry");
    match trusted_dir(&registry, uid) {
        Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(err) => return Err(err),
        Ok(()) => {}
    }
    let mut result = Vec::new();
    for item in fs::read_dir(registry)? {
        let item = item?;
        let path = item.path();
        if path.extension().is_none_or(|ext| ext != "json") {
            continue;
        }
        if let Some(session) = read_entry(&path, &runtime, uid) {
            result.push(session);
        }
    }
    result.sort_by(|a, b| {
        b.started_at
            .cmp(&a.started_at)
            .then_with(|| a.id.cmp(&b.id))
    });
    Ok(result)
}

fn read_entry(path: &Path, runtime: &Path, uid: u32) -> Option<Session> {
    let path_meta = fs::symlink_metadata(path).ok()?;
    if !path_meta.file_type().is_file()
        || path_meta.uid() != uid
        || path_meta.permissions().mode() & 0o077 != 0
    {
        return None;
    }
    let mut file = File::open(path).ok()?;
    let meta = file.metadata().ok()?;
    if !meta.file_type().is_file()
        || meta.ino() != path_meta.ino()
        || meta.dev() != path_meta.dev()
        || meta.uid() != uid
        || meta.permissions().mode() & 0o077 != 0
        || meta.len() > MAX_ENTRY_BYTES
    {
        return None;
    }
    let mut bytes = Vec::new();
    file.by_ref()
        .take(MAX_ENTRY_BYTES + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    if bytes.len() as u64 > MAX_ENTRY_BYTES {
        return None;
    }
    let entry: Entry = serde_json::from_slice(&bytes).ok()?;
    if !valid_id(&entry.session_id)
        || path.file_stem()?.to_str()? != entry.session_id
        || entry.pid <= 0
        || !pid_alive(entry.pid)
        || !fresh(&entry.heartbeat_at)
    {
        return None;
    }
    let socket = PathBuf::from(entry.daemon_socket?);
    // The daemon writes this exact filename under the runtime directory.
    // Do not follow a registry-supplied path outside that boundary.
    let expected = format!(
        "daemon-{}-{}.sock",
        &entry.session_id[..entry.session_id.len().min(8)],
        entry.pid
    );
    if !socket.is_absolute()
        || socket.file_name()?.to_str()? != expected
        || socket.parent()? != runtime
    {
        return None;
    }
    let socket_meta = fs::symlink_metadata(&socket).ok()?;
    if !socket_meta.file_type().is_socket()
        || socket_meta.uid() != uid
        || socket_meta.permissions().mode() & 0o077 != 0
    {
        return None;
    }
    Some(Session {
        id: entry.session_id,
        socket,
        clients: entry.clients,
        started_at: entry.started_at,
    })
}

fn pid_alive(pid: i32) -> bool {
    let status = unsafe { libc::kill(pid, 0) };
    status == 0 || io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

fn fresh(value: &str) -> bool {
    OffsetDateTime::parse(value, &Rfc3339)
        .map(|when| (OffsetDateTime::now_utc() - when).whole_seconds() <= STALE_SECS)
        .unwrap_or(false)
}

/// Same filename-safe shape as Python's `identity.valid_session_id`.
pub fn valid_id(id: &str) -> bool {
    id.len() <= 128
        && id.as_bytes().first().is_some_and(u8::is_ascii_alphanumeric)
        && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}

/// Full ID wins, then an unambiguous prefix. Never guess among candidates.
pub fn select<'a>(entries: &'a [Session], prefix: Option<&str>) -> io::Result<&'a Session> {
    if let Some(prefix) = prefix {
        if !valid_id(prefix) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid session ID",
            ));
        }
        if let Some(exact) = entries.iter().find(|s| s.id == prefix) {
            return Ok(exact);
        }
    }
    let matches: Vec<_> = entries
        .iter()
        .filter(|s| prefix.is_none_or(|p| s.id.starts_with(p)))
        .collect();
    match matches.as_slice() {
        [one] => Ok(one),
        [] => Err(io::Error::new(
            io::ErrorKind::NotFound,
            "no matching live daemon session; use --list",
        )),
        _ => Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "multiple live daemon sessions match; use --list and --session ID",
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::UnixListener;

    #[test]
    fn id_shape_matches_python_filename_rule() {
        for id in ["a", "AB-123", &"a".repeat(128)] {
            assert!(valid_id(id));
        }
        for id in ["", "-a", "../a", "a_b", "a\\b", &"a".repeat(129)] {
            assert!(!valid_id(id));
        }
    }

    #[test]
    fn selection_never_guesses() {
        let entries = ["abc123", "abc456"].map(|id| Session {
            id: id.into(),
            socket: PathBuf::new(),
            clients: None,
            started_at: String::new(),
        });
        assert_eq!(select(&entries, Some("abc123")).unwrap().id, "abc123");
        assert_eq!(select(&entries, Some("abc4")).unwrap().id, "abc456");
        assert!(select(&entries, Some("abc")).is_err());
        assert!(select(&entries, None).is_err());
        assert!(select(&entries, Some("../abc")).is_err());
    }

    #[test]
    fn registry_entry_requires_live_owned_socket_and_fresh_heartbeat() {
        let dir = tempfile::tempdir().unwrap();
        let runtime = dir.path();
        let socket = runtime.join(format!("daemon-test-{}.sock", std::process::id()));
        let _listener = UnixListener::bind(&socket).unwrap();
        fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
        let path = runtime.join("test.json");
        let mut entry = serde_json::json!({
            "session_id": "test", "pid": std::process::id(),
            "heartbeat_at": OffsetDateTime::now_utc().format(&Rfc3339).unwrap(),
            "started_at": "2026-01-01T00:00:00.000000Z",
            "title": "running", "daemon_socket": socket.to_str().unwrap(),
        });
        let write = |entry: &serde_json::Value| {
            fs::write(&path, serde_json::to_vec(entry).unwrap()).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        };
        let uid = unsafe { libc::geteuid() };
        write(&entry);
        assert_eq!(read_entry(&path, runtime, uid).unwrap().id, "test");
        entry["heartbeat_at"] = "2000-01-01T00:00:00.000000Z".into();
        write(&entry);
        assert!(read_entry(&path, runtime, uid).is_none());
        entry["heartbeat_at"] = OffsetDateTime::now_utc().format(&Rfc3339).unwrap().into();
        entry["daemon_socket"] = "/tmp/other.sock".into();
        write(&entry);
        assert!(read_entry(&path, runtime, uid).is_none());
        entry["daemon_socket"] = socket.to_str().unwrap().into();
        write(&entry);
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(read_entry(&path, runtime, uid).is_none());
        fs::remove_file(&path).unwrap();
        std::os::unix::fs::symlink(&socket, &path).unwrap();
        assert!(read_entry(&path, runtime, uid).is_none());
    }
}

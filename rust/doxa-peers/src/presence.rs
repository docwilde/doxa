//! Read-only, same-scope presence projection for daemon RPCs.
//! No reaping or registry writes occur during this query.

use crate::{pid_alive, read_one, safe_id, stale, MAX_REGISTRY_ENTRIES};
use serde::Serialize;
use std::ffi::OsStr;
use std::fs::{self, Metadata};
use std::io;
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Component, Path};

pub const MAX_DISPLAY_PEERS: usize = 32;
const MAX_TITLE_CHARS: usize = 64;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DisplayPeer {
    pub session_id: String,
    pub title: String,
}

fn trusted_dir(path: &Path) -> io::Result<bool> {
    let meta = match fs::symlink_metadata(path) {
        Ok(meta) => meta,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error),
    };
    if !meta.file_type().is_dir()
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.permissions().mode() & 0o077 != 0
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "unsafe peer registry directory",
        ));
    }
    Ok(true)
}

fn safe_socket(runtime: &Path, path: &Path) -> bool {
    if !path.is_absolute()
        || path.parent() != Some(runtime)
        || path
            .components()
            .any(|part| !matches!(part, Component::RootDir | Component::Normal(_)))
    {
        return false;
    }
    let Ok(meta) = fs::symlink_metadata(path) else {
        return false;
    };
    meta.file_type().is_socket()
        && meta.uid() == unsafe { libc::geteuid() }
        && meta.permissions().mode() & 0o077 == 0
}

fn safe_entry(meta: &Metadata) -> bool {
    meta.file_type().is_file()
        && meta.uid() == unsafe { libc::geteuid() }
        && meta.nlink() == 1
        && meta.permissions().mode() & 0o077 == 0
}

fn title(text: String, id: &str) -> String {
    let clean: String = text
        .chars()
        .map(|ch| if ch.is_control() { ' ' } else { ch })
        .take(MAX_TITLE_CHARS)
        .collect();
    if clean.trim().is_empty() {
        id.chars().take(12).collect()
    } else {
        clean
    }
}

/// Discover live peers from owner-private files without modifying registry
/// state. Scope is checked against raw metadata before any display scrubbing.
/// A scrub failure rejects the whole result; no raw title can escape.
pub fn list_scoped_readonly(
    runtime: &Path,
    scope: &str,
    self_id: &str,
    mut scrub: impl FnMut(&str) -> io::Result<String>,
) -> io::Result<Vec<DisplayPeer>> {
    if !runtime.is_absolute() || scope.is_empty() || !safe_id(self_id) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "invalid peer query",
        ));
    }
    if !trusted_dir(runtime)? {
        return Ok(Vec::new());
    }
    let directory = runtime.join("registry");
    if !trusted_dir(&directory)? {
        return Ok(Vec::new());
    }
    let mut paths: Vec<_> = fs::read_dir(directory)?
        .take(MAX_REGISTRY_ENTRIES)
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.extension() == Some(OsStr::new("json")))
        .collect();
    paths.sort();
    let mut result = Vec::new();
    for path in paths {
        let Ok(meta) = fs::symlink_metadata(&path) else {
            continue;
        };
        if !safe_entry(&meta) {
            continue;
        }
        let Ok(peer) = read_one(&path) else {
            continue;
        };
        if path.file_stem() != Some(OsStr::new(&peer.session_id))
            || !safe_id(&peer.session_id)
            || peer.session_id == self_id
            || peer.scope_key() != scope
            || !pid_alive(peer.pid)
            || stale(&peer.heartbeat_at)
            || !safe_socket(runtime, Path::new(&peer.socket_path))
            || UnixStream::connect(&peer.socket_path).is_err()
        {
            continue;
        }
        let display = title(scrub(&peer.title)?, &peer.session_id);
        result.push(DisplayPeer {
            session_id: peer.session_id,
            title: display,
        });
        if result.len() == MAX_DISPLAY_PEERS {
            break;
        }
    }
    Ok(result)
}

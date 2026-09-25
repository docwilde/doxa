// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded, read-only queries over Python's append-only peer ledger.
use crate::delivery::{Ledger, Message};
use std::collections::VecDeque;
use std::ffi::CString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::Path;

pub const DEFAULT_QUERY_LIMIT: usize = 50;
pub const MAX_QUERY_RESULTS: usize = 5_000;
pub const MAX_QUERY_LINE_BYTES: usize = 8 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QueryStats {
    /// Complete, parseable messages observed at this file snapshot.
    pub count: u64,
    /// Complete lines that could not be parsed, including oversized lines.
    pub malformed_lines: u64,
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn limit(value: usize) -> io::Result<usize> {
    if value > MAX_QUERY_RESULTS {
        Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "peer ledger query limit exceeds 5000",
        ))
    } else {
        Ok(value)
    }
}

/// The file is read through one descriptor and a fixed byte boundary. A
/// writer's unfinished final line is ignored until it gains a newline.
fn scan(path: &Path, ceiling: u64, mut visit: impl FnMut(Message)) -> io::Result<QueryStats> {
    let path_meta = match fs::symlink_metadata(path) {
        Ok(meta) => meta,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Ok(QueryStats {
                count: 0,
                malformed_lines: 0,
            });
        }
        Err(error) => return Err(error),
    };
    let uid = unsafe { libc::geteuid() };
    let parent = path
        .parent()
        .ok_or_else(|| invalid("peer ledger has no directory"))?;
    let dir_meta = fs::symlink_metadata(parent)?;
    if !dir_meta.file_type().is_dir()
        || dir_meta.uid() != uid
        || dir_meta.permissions().mode() & 0o077 != 0
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "unsafe peer ledger directory",
        ));
    }
    if !path_meta.file_type().is_file()
        || path_meta.uid() != uid
        || path_meta.permissions().mode() & 0o077 != 0
        || path_meta.nlink() != 1
        || path_meta.len() > ceiling
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "unsafe peer ledger file",
        ));
    }
    let directory = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(parent)?;
    let opened_dir = directory.metadata()?;
    if opened_dir.dev() != dir_meta.dev()
        || opened_dir.ino() != dir_meta.ino()
        || opened_dir.uid() != uid
        || opened_dir.permissions().mode() & 0o077 != 0
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "peer ledger directory changed during open",
        ));
    }
    let name = CString::new(
        path.file_name()
            .ok_or_else(|| invalid("peer ledger has no filename"))?
            .as_bytes(),
    )
    .map_err(|_| invalid("peer ledger filename contains NUL"))?;
    let fd = unsafe {
        libc::openat(
            directory.as_raw_fd(),
            name.as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let mut file = unsafe { File::from_raw_fd(fd) };
    let opened = file.metadata()?;
    if opened.dev() != path_meta.dev()
        || opened.ino() != path_meta.ino()
        || !opened.file_type().is_file()
        || opened.uid() != uid
        || opened.nlink() != 1
        || opened.permissions().mode() & 0o077 != 0
        || opened.len() > ceiling
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "peer ledger changed during open",
        ));
    }
    let mut remaining = opened.len();
    let mut line = Vec::new();
    let mut oversized = false;
    let mut stats = QueryStats {
        count: 0,
        malformed_lines: 0,
    };
    let mut block = [0_u8; 8192];
    while remaining > 0 {
        let take = block.len().min(remaining as usize);
        let n = file.read(&mut block[..take])?;
        if n == 0 {
            break;
        }
        remaining -= n as u64;
        for byte in &block[..n] {
            if *byte == b'\n' {
                if oversized {
                    stats.malformed_lines += 1;
                } else if !line.iter().all(u8::is_ascii_whitespace) {
                    match serde_json::from_slice::<Message>(&line) {
                        Ok(message) if valid_message(&message) => {
                            stats.count += 1;
                            visit(message);
                        }
                        _ => stats.malformed_lines += 1,
                    }
                }
                line.clear();
                oversized = false;
            } else if !oversized {
                if line.len() == MAX_QUERY_LINE_BYTES {
                    line.clear();
                    oversized = true;
                } else {
                    line.push(*byte);
                }
            }
        }
    }
    Ok(stats)
}

fn valid_message(message: &Message) -> bool {
    !message.id.is_empty()
        && !message.sender.session.trim().is_empty()
        && matches!(message.kind.as_str(), "direct" | "broadcast")
}

impl Ledger {
    /// Count all parseable complete lines. Malformed lines remain visible in
    /// the returned stats instead of silently reducing the count.
    pub fn query_stats(&self) -> io::Result<QueryStats> {
        scan(&self.path, self.ceiling, |_| {})
    }

    pub fn latest_id(&self) -> io::Result<Option<String>> {
        let mut latest = None;
        scan(&self.path, self.ceiling, |message| {
            latest = Some(message.id)
        })?;
        Ok(latest)
    }

    fn newest(&self, max: usize, predicate: impl Fn(&Message) -> bool) -> io::Result<Vec<Message>> {
        let max = limit(max)?;
        if max == 0 {
            return Ok(Vec::new());
        }
        let mut tail = VecDeque::with_capacity(max);
        scan(&self.path, self.ceiling, |message| {
            if predicate(&message) {
                if tail.len() == max {
                    tail.pop_front();
                }
                tail.push_back(message);
            }
        })?;
        Ok(tail.into_iter().rev().collect())
    }

    /// Newest first, like Python's `recent`.
    pub fn recent(&self, limit: usize) -> io::Result<Vec<Message>> {
        self.newest(limit, |_| true)
    }

    pub fn in_repo(&self, repo: &str, limit: usize) -> io::Result<Vec<Message>> {
        self.newest(limit, |message| {
            message.sender.repo.as_deref() == Some(repo)
        })
    }

    pub fn sent_by(&self, session_id: &str, limit: usize) -> io::Result<Vec<Message>> {
        self.newest(limit, |message| message.sender.session == session_id)
    }

    pub fn received_by(&self, session_id: &str, limit: usize) -> io::Result<Vec<Message>> {
        self.newest(limit, |message| {
            message.to.iter().any(|id| id == session_id)
        })
    }

    /// Oldest first after an exact cursor. An unknown cursor is an error,
    /// never a replay from the beginning. The cursor is checked even if a
    /// session filter would exclude that message.
    pub fn since(
        &self,
        cursor: Option<&str>,
        session_id: Option<&str>,
        max: usize,
    ) -> io::Result<Vec<Message>> {
        let max = limit(max)?;
        let mut seen = cursor.is_none();
        let mut results = Vec::new();
        scan(&self.path, self.ceiling, |message| {
            if !seen {
                seen = Some(message.id.as_str()) == cursor;
            } else if results.len() < max
                && session_id.is_none_or(|id| {
                    message.sender.session == id || message.to.iter().any(|to| to == id)
                })
            {
                results.push(message);
            }
        })?;
        if !seen {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "unknown peer ledger cursor",
            ));
        }
        Ok(results)
    }
}

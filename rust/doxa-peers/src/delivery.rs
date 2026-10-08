// SPDX-License-Identifier: AGPL-3.0-only
//! Local peer transport and append-only delivery evidence. The caller supplies a LORE scrubber.
use crate::{now, PeerRecord, Registry, Scrubber};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::VecDeque;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::fd::AsRawFd;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{DirBuilderExt, FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use uuid::Uuid;

pub const MAX_FRAME_BYTES: usize = 64 * 1024;
pub const MAX_LEDGER_BYTES: u64 = 2 * 1024 * 1024 * 1024;
pub const MAX_BODY_CHARS: usize = 8_000;
const TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct PeerFrame {
    /// Kernel-observed sender identity; serialized claims never fill this field.
    #[serde(skip)]
    pub authenticated_pid: Option<i32>,
    pub from_id: String,
    pub from_title: String,
    pub sent_at: String,
    pub body: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub from_repo: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub kind: Option<String>,
}
impl PeerFrame {
    fn scrub(mut self, scrubber: &impl Scrubber) -> Self {
        self.from_id = scrubber.scrub(&self.from_id);
        self.from_title = scrubber.scrub(&self.from_title);
        self.sent_at = scrubber.scrub(&self.sent_at);
        self.body = scrubber.scrub(&self.body);
        self.from_repo = self.from_repo.map(|s| scrubber.scrub(&s));
        self.kind = self.kind.map(|s| scrubber.scrub(&s));
        self
    }
}

fn invalid(message: &'static str) -> io::Error { io::Error::new(io::ErrorKind::InvalidData, message) }
fn same_user(stream: &UnixStream) -> io::Result<()> {
    if crate::credentials::peer_uid(stream)? != unsafe { libc::geteuid() } {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "peer UID differs"));
    }
    Ok(())
}
fn read_frame(stream: &mut UnixStream) -> io::Result<PeerFrame> {
    let deadline = Instant::now() + TIMEOUT;
    let mut bytes = Vec::new();
    stream.set_nonblocking(true)?;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() { return Err(io::Error::new(io::ErrorKind::TimedOut, "peer frame timed out")); }
        let mut buf = [0u8; 4096];
        let n = match stream.read(&mut buf) {
            Ok(n) => n,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                let mut ready = libc::pollfd { fd: stream.as_raw_fd(), events: libc::POLLIN, revents: 0 };
                let millis = remaining.as_millis().max(1).min(i32::MAX as u128) as i32;
                let result = unsafe { libc::poll(&mut ready, 1, millis) };
                if result == 0 { return Err(io::Error::new(io::ErrorKind::TimedOut, "peer frame timed out")); }
                if result < 0 {
                    let error = io::Error::last_os_error();
                    if error.kind() == io::ErrorKind::Interrupted { continue; }
                    return Err(error);
                }
                continue;
            }
            Err(error) => return Err(error),
        };
        if n == 0 { break; }
        bytes.extend_from_slice(&buf[..n]);
        if bytes.len() > MAX_FRAME_BYTES { return Err(invalid("peer frame too large")); }
        if bytes.contains(&b'\n') { break; }
    }
    parse_frame(&bytes)
}
fn parse_frame(bytes: &[u8]) -> io::Result<PeerFrame> {
    if bytes.is_empty() { return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "empty peer probe")); }
    if bytes.last() != Some(&b'\n') || bytes[..bytes.len()-1].contains(&b'\n') { return Err(invalid("peer frame must be one complete line")); }
    let frame: PeerFrame = serde_json::from_slice(&bytes[..bytes.len()-1]).map_err(|_| invalid("invalid peer JSON"))?;
    if frame.from_id.is_empty() || frame.body.chars().count() > MAX_BODY_CHARS { return Err(invalid("invalid peer identity or body")); }
    Ok(frame)
}

/// A receiving socket is created exclusively. An existing path, even a dead socket, is never unlinked by this API.
struct PendingFrame { pid: i32, stream: UnixStream, bytes: Vec<u8>, started: Instant }
pub struct Inbox { listener: UnixListener, path: PathBuf, inode: u64, device: u64, pending: Mutex<Option<PendingFrame>> }
impl Inbox {
    pub fn bind(runtime: &Path, session_id: &str) -> io::Result<Self> {
        if session_id.is_empty() || !session_id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid session id"));
        }
        let meta = fs::symlink_metadata(runtime)?;
        if !meta.file_type().is_dir() || meta.uid() != unsafe { libc::geteuid() } || meta.permissions().mode() & 0o077 != 0 {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "unsafe runtime directory"));
        }
        let path = runtime.join(format!("peer-{}-{}.sock", &session_id[..session_id.len().min(8)], std::process::id()));
        if path.as_os_str().as_bytes().len() >= 100 { return Err(io::Error::new(io::ErrorKind::InvalidInput, "socket path too long")); }
        if fs::symlink_metadata(&path).is_ok() { return Err(io::Error::new(io::ErrorKind::AlreadyExists, "peer socket already exists")); }
        let listener = UnixListener::bind(&path)?;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
        let meta = fs::symlink_metadata(&path)?;
        Ok(Self { listener, path, inode: meta.ino(), device: meta.dev(), pending: Mutex::new(None) })
    }
    pub fn path(&self) -> &Path { &self.path }
    /// Poll one connection without blocking daemon shutdown. Partial frames
    /// are retained across polls; an empty discovery probe has no message.
    pub fn poll_receive(&self, scrubber: &impl Scrubber) -> io::Result<Option<PeerFrame>> {
        let mut pending = self.pending.lock().map_err(|_| io::Error::other("peer inbox lock poisoned"))?;
        if pending.is_none() {
            self.listener.set_nonblocking(true)?;
            let (stream, _) = match self.listener.accept() {
                Ok(value) => value,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => return Ok(None),
                Err(error) => return Err(error),
            };
            same_user(&stream)?;
            let pid = crate::credentials::peer_credentials(&stream).map(|credentials|credentials.pid).unwrap_or(0);
            stream.set_nonblocking(true)?;
            *pending = Some(PendingFrame { pid, stream, bytes: Vec::new(), started: Instant::now() });
        }
        let frame = pending.as_mut().expect("pending peer connection");
        if frame.started.elapsed() >= TIMEOUT {
            *pending = None;
            return Err(io::Error::new(io::ErrorKind::TimedOut, "peer frame timed out"));
        }
        loop {
            let mut buf = [0u8; 4096];
            let n = match frame.stream.read(&mut buf) {
                Ok(n) => n,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => return Ok(None),
                Err(error) => { *pending = None; return Err(error); }
            };
            if n > 0 { frame.bytes.extend_from_slice(&buf[..n]); }
            if frame.bytes.len() > MAX_FRAME_BYTES {
                *pending = None;
                return Err(invalid("peer frame too large"));
            }
            if n == 0 || frame.bytes.contains(&b'\n') {
                let bytes = std::mem::take(&mut frame.bytes);
                let authenticated_pid = Some(frame.pid);
                *pending = None;
                return match parse_frame(&bytes) {
                    Ok(mut frame) => { frame.authenticated_pid = authenticated_pid; Ok(Some(frame.scrub(scrubber))) },
                    Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => Ok(None),
                    Err(error) => Err(error),
                };
            }
        }
    }
    pub fn receive(&self, scrubber: &impl Scrubber) -> io::Result<PeerFrame> {
        self.listener.set_nonblocking(false)?;
        loop {
            let (mut stream, _) = self.listener.accept()?;
            same_user(&stream)?;
            match read_frame(&mut stream) {
                Ok(mut frame) => { frame.authenticated_pid = crate::credentials::peer_credentials(&stream).ok().map(|credentials|credentials.pid); return Ok(frame.scrub(scrubber)); },
                Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => continue, // discovery probe
                Err(e) => return Err(e),
            }
        }
    }
}
impl Drop for Inbox {
    fn drop(&mut self) {
        // Avoid deleting a replacement path installed after this listener bound.
        if let Ok(current) = fs::symlink_metadata(&self.path) {
            if current.file_type().is_socket() && current.ino() == self.inode && current.dev() == self.device {
                let _ = fs::remove_file(&self.path);
            }
        }
    }
}
fn connect_before(path: &Path, deadline: Instant) -> io::Result<UnixStream> {
    let bytes = path.as_os_str().as_bytes();
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    if bytes.is_empty() || bytes.contains(&0) || bytes.len() >= address.sun_path.len() {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid peer socket path"));
    }
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (slot, byte) in address.sun_path.iter_mut().zip(bytes) { *slot = *byte as libc::c_char; }
    let size = (std::mem::offset_of!(libc::sockaddr_un, sun_path) + bytes.len() + 1) as libc::socklen_t;
    #[cfg(target_os = "macos")]
    { address.sun_len = size as u8; }
    loop {
        if deadline.saturating_duration_since(Instant::now()).is_zero() {
            return Err(io::Error::new(io::ErrorKind::TimedOut, "peer connect timed out"));
        }
        let fd = crate::credentials::nonblocking_unix_socket()?;
        let result = unsafe { libc::connect(fd.as_raw_fd(), (&address as *const libc::sockaddr_un).cast(), size) };
        if result < 0 {
            let error = io::Error::last_os_error();
            // Linux reports EAGAIN, rather than EINPROGRESS, when a
            // nonblocking AF_UNIX listener's queue is full. A fresh socket
            // can connect as soon as the listener accepts a queued peer.
            if error.kind() == io::ErrorKind::WouldBlock
                || (cfg!(target_os = "macos") && error.raw_os_error() == Some(libc::ECONNREFUSED)) {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() { return Err(io::Error::new(io::ErrorKind::TimedOut, "peer connect timed out")); }
                std::thread::sleep(remaining.min(Duration::from_millis(10)));
                continue;
            }
            if error.raw_os_error() != Some(libc::EINPROGRESS) { return Err(error); }
            loop {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() { return Err(io::Error::new(io::ErrorKind::TimedOut, "peer connect timed out")); }
                let mut ready = libc::pollfd { fd: fd.as_raw_fd(), events: libc::POLLOUT, revents: 0 };
                let millis = remaining.as_millis().max(1).min(i32::MAX as u128) as i32;
                let result = unsafe { libc::poll(&mut ready, 1, millis) };
                if result == 0 { return Err(io::Error::new(io::ErrorKind::TimedOut, "peer connect timed out")); }
                if result < 0 {
                    let error = io::Error::last_os_error();
                    if error.kind() == io::ErrorKind::Interrupted { continue; }
                    return Err(error);
                }
                let mut socket_error = 0;
                let mut error_size = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
                if unsafe { libc::getsockopt(fd.as_raw_fd(), libc::SOL_SOCKET, libc::SO_ERROR,
                    (&mut socket_error as *mut libc::c_int).cast(), &mut error_size) } < 0 {
                    return Err(io::Error::last_os_error());
                }
                if socket_error != 0 { return Err(io::Error::from_raw_os_error(socket_error)); }
                break;
            }
        }
        let stream = UnixStream::from(fd);
        stream.set_nonblocking(false)?;
        return Ok(stream);
    }
}

#[cfg(target_os = "macos")]
pub(crate) fn probe_socket(path: &Path) -> bool {
    connect_before(path, Instant::now() + Duration::from_millis(100)).is_ok()
}

pub fn send(path: &Path, frame: &PeerFrame) -> io::Result<()> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.file_type().is_socket() || meta.uid() != unsafe { libc::geteuid() }
        || meta.permissions().mode() & 0o077 != 0 { return Err(invalid("unsafe peer socket")); }
    let mut bytes = serde_json::to_vec(frame).map_err(io::Error::other)?;
    bytes.push(b'\n');
    if bytes.len() > MAX_FRAME_BYTES { return Err(invalid("peer frame too large")); }
    let deadline = Instant::now() + TIMEOUT;
    let mut stream = connect_before(path, deadline).map_err(|error|
        io::Error::new(error.kind(), format!("peer connect: {error}")))?;
    same_user(&stream).map_err(|error|
        io::Error::new(error.kind(), format!("peer credentials: {error}")))?;
    let mut remaining_bytes = bytes.as_slice();
    while !remaining_bytes.is_empty() {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() { return Err(io::Error::new(io::ErrorKind::TimedOut, "peer write timed out")); }
        stream.set_write_timeout(Some(remaining)).map_err(|error|
            io::Error::new(error.kind(), format!("peer write timeout setup: {error}")))?;
        match stream.write(remaining_bytes) {
            Ok(0) => return Err(io::Error::new(io::ErrorKind::WriteZero, "peer write returned zero")),
            Ok(n) => remaining_bytes = &remaining_bytes[n..],
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(io::Error::new(error.kind(), format!("peer write: {error}"))),
        }
    }
    Ok(())
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Sender { pub session: String, pub title: Option<String>, pub repo: Option<String>, pub model: Option<String>, pub engine: Option<String> }
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TurnRef { pub id: Option<String>, pub state: String }
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Message {
    pub v: u8, pub id: String, pub ts: String,
    #[serde(rename="from")] pub sender: Sender,
    pub to: Vec<String>, pub kind: String, pub in_reply_to: Option<String>,
    pub body: String, pub body_sha256: String, pub latency_ms: Option<u64>, pub turn: TurnRef,
}
pub struct Ledger { pub(crate) path: PathBuf, pub(crate) ceiling: u64 }
impl Ledger {
    pub fn new(path: PathBuf) -> Self { Self { path, ceiling: MAX_LEDGER_BYTES } }
    pub fn with_ceiling(path: PathBuf, ceiling: u64) -> Self { Self { path, ceiling } }
    /// Read a bounded tail from one private inode. Only messages involving
    /// this session in the exact project scope are returned. A partial first
    /// line or concurrently appended incomplete final line is ignored.
    pub fn history(&self, session: &str, scope: &str, scrubber: &impl Scrubber) -> io::Result<Vec<Message>> {
        self.history_bounded(session,scope,"both",20,24*1024,scrubber)
    }
    /// Filters apply before the row limit, within the same bounded private tail.
    pub fn history_filtered(&self, session: &str, scope: &str, direction: &str, limit: usize, scrubber: &impl Scrubber) -> io::Result<Vec<Message>> {
        self.history_bounded(session,scope,direction,limit,48*1024,scrubber)
    }
    fn history_bounded(&self, session: &str, scope: &str, direction: &str, limit: usize, output_limit: usize, scrubber: &impl Scrubber) -> io::Result<Vec<Message>> {
        if !matches!(direction,"both"|"sent"|"received") || !(1..=100).contains(&limit) { return Err(invalid("invalid peer history filter")); }
        let parent = self.path.parent().ok_or_else(|| invalid("ledger has no parent"))?;
        let meta = match fs::symlink_metadata(parent) {
            Ok(meta) => meta,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(error) => return Err(error),
        };
        if !meta.is_dir() || meta.file_type().is_symlink() || meta.uid() != unsafe { libc::geteuid() }
            || meta.mode() & 0o077 != 0 { return Err(invalid("unsafe ledger directory")); }
        let dir = OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC).open(parent)?;
        let opened_dir = dir.metadata()?;
        if (opened_dir.dev(), opened_dir.ino()) != (meta.dev(), meta.ino())
            || opened_dir.uid() != unsafe {libc::geteuid()} || opened_dir.mode() & 0o077 != 0 {
            return Err(invalid("ledger directory changed during open"));
        }
        use std::ffi::CString;
        use std::os::unix::ffi::OsStrExt;
        use std::os::fd::FromRawFd;
        let name = CString::new(self.path.file_name().ok_or_else(|| invalid("invalid ledger name"))?.as_bytes())
            .map_err(|_| invalid("invalid ledger name"))?;
        let fd = unsafe { libc::openat(dir.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK) };
        if fd < 0 {
            let error = io::Error::last_os_error();
            return if error.kind() == io::ErrorKind::NotFound { Ok(Vec::new()) } else { Err(error) };
        }
        let mut file = unsafe { std::fs::File::from_raw_fd(fd) };
        let meta = file.metadata()?;
        if !meta.is_file() || meta.uid() != unsafe { libc::geteuid() } || meta.nlink() != 1
            || meta.mode() & 0o077 != 0 { return Err(invalid("unsafe ledger file")); }
        const TAIL: u64 = 256 * 1024;
        let offset = meta.len().saturating_sub(TAIL);
        file.seek(SeekFrom::Start(offset))?;
        let mut bytes = Vec::new(); file.take(TAIL).read_to_end(&mut bytes)?;
        let start = if offset > 0 { bytes.iter().position(|b| *b == b'\n').map(|at| at + 1).unwrap_or(bytes.len()) } else { 0 };
        let end = bytes.iter().rposition(|b| *b == b'\n').unwrap_or(0);
        if start >= end { return Ok(Vec::new()); }
        let mut result = Vec::new(); let mut output_bytes = 0usize;
        for line in bytes[start..end].split(|b| *b == b'\n').rev() {
            if line.len() > 32 * 1024 { continue; }
            let Ok(mut message) = serde_json::from_slice::<Message>(line) else { continue; };
            if message.sender.repo.as_deref() != Some(scope)
                || (message.sender.session != session && !message.to.iter().any(|target| target == session))
                || (direction == "sent" && message.sender.session != session)
                || (direction == "received" && !message.to.iter().any(|target| target == session)) { continue; }
            message.body = scrubber.scrub(&message.body);
            for value in [&mut message.sender.title, &mut message.sender.repo, &mut message.sender.model, &mut message.sender.engine] {
                *value = value.take().map(|value| scrubber.scrub(&value));
            }
            let length = serde_json::to_vec(&message).map_err(io::Error::other)?.len();
            if output_bytes.saturating_add(length) > output_limit { break; }
            output_bytes += length; result.push(message);
            if result.len() == limit { break; }
        }
        result.reverse(); Ok(result)
    }
    pub fn append(&self, mut message: Message, scrubber: &impl Scrubber) -> io::Result<Message> {
        if message.to.is_empty() || !matches!(message.kind.as_str(), "direct" | "broadcast") { return Err(invalid("invalid ledger delivery")); }
        message.body_sha256 = format!("{:x}", Sha256::digest(message.body.as_bytes()));
        message.body = scrubber.scrub(&message.body);
        for value in [&mut message.sender.title, &mut message.sender.repo, &mut message.sender.model, &mut message.sender.engine] {
            *value = value.take().map(|s| scrubber.scrub(&s));
        }
        let mut line = serde_json::to_vec(&message).map_err(io::Error::other)?;
        line.push(b'\n');
        let parent = self.path.parent().ok_or_else(|| invalid("ledger has no parent"))?;
        let mut builder = fs::DirBuilder::new();
        builder.recursive(true).mode(0o700).create(parent)?;
        let parent_meta = fs::symlink_metadata(parent)?;
        if !parent_meta.file_type().is_dir() || parent_meta.uid() != unsafe { libc::geteuid() } { return Err(invalid("unsafe ledger directory")); }
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
        let mut file = OpenOptions::new().write(true).create(true).append(true).mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK).open(&self.path)?;
        let meta = file.metadata()?;
        if !meta.file_type().is_file() || meta.uid() != unsafe { libc::geteuid() } || meta.nlink() != 1 { return Err(invalid("unsafe ledger file")); }
        if meta.permissions().mode() & 0o777 != 0o600 {
            file.set_permissions(fs::Permissions::from_mode(0o600))?;
        }
        let lock_deadline = Instant::now() + TIMEOUT;
        loop {
            if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 { break; }
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::WouldBlock { return Err(error); }
            if Instant::now() >= lock_deadline {
                return Err(io::Error::new(io::ErrorKind::TimedOut, "peer ledger lock timed out"));
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let size = file.metadata()?.len();
        if size.saturating_add(line.len() as u64) > self.ceiling { return Err(io::Error::new(io::ErrorKind::OutOfMemory, "peer ledger full")); }
        file.write_all(&line)?;
        file.sync_data()?;
        Ok(message)
    }
}

#[derive(Clone, Copy)]
pub struct SendLimits { pub per_turn: u32, pub per_window: u32, pub window: Duration }
impl Default for SendLimits { fn default() -> Self { Self { per_turn: 64, per_window: 512, window: Duration::from_secs(60) } } }
struct Charge { at: Instant, count: u32, turn: Option<String> }
pub struct RateLimiter { limits: SendLimits, history: VecDeque<Charge>, current_turn: Option<String> }
impl RateLimiter {
    pub fn new(limits: SendLimits) -> Self { Self { limits, history: VecDeque::new(), current_turn: None } }
    pub fn active(&self) -> bool { self.history.back().is_some_and(|charge|charge.at.elapsed()<self.limits.window) }
    pub fn charge(&mut self, turn: Option<&str>, fanout: usize) -> io::Result<()> {
        let count = u32::try_from(fanout).map_err(|_| invalid("fanout too large"))?;
        if count == 0 { return Err(invalid("empty fanout")); }
        let now = Instant::now();
        if let Some(turn) = turn { self.current_turn = Some(turn.to_owned()); }
        let live = self.current_turn.as_deref();
        self.history.retain(|c| now.duration_since(c.at) < self.limits.window || (live.is_some() && c.turn.as_deref() == live));
        let turn_used: u32 = self.history.iter().filter(|c| turn.is_some() && c.turn.as_deref() == turn).map(|c| c.count).sum();
        let window_used: u32 = self.history.iter().filter(|c| now.duration_since(c.at) < self.limits.window).map(|c| c.count).sum();
        if turn.is_some() && turn_used.saturating_add(count) > self.limits.per_turn { return Err(io::Error::new(io::ErrorKind::WouldBlock, "peer turn send limit")); }
        if window_used.saturating_add(count) > self.limits.per_window { return Err(io::Error::new(io::ErrorKind::WouldBlock, "peer window send limit")); }
        self.history.push_back(Charge { at: now, count, turn: turn.map(str::to_owned) });
        Ok(())
    }
}

pub struct DeliveryResult { pub delivered: Vec<String>, pub failed: Vec<String>, pub record: Option<Message>, pub ledger_error: Option<String> }
pub fn valid_reply_reference(id: &str) -> bool { Uuid::parse_str(id).is_ok() }
pub fn new_message_id() -> String { Uuid::new_v4().simple().to_string() }
/// The single local outbound path: scoped discovery, charge, send, then append only successful recipients.
pub fn deliver(registry: &Registry, sender: &PeerRecord, recipients: &[String], body: &str, kind: &str,
    turn_id: Option<&str>, limiter: &Mutex<RateLimiter>, ledger: &Ledger, scrubber: &impl Scrubber) -> io::Result<DeliveryResult> {
    deliver_with_reply(registry,sender,recipients,body,kind,turn_id,None,limiter,ledger,scrubber)
}
/// Thread evidence is appended by the same charged delivery path, only after delivery.
pub fn deliver_with_reply(registry: &Registry, sender: &PeerRecord, recipients: &[String], body: &str, kind: &str,
    turn_id: Option<&str>, in_reply_to: Option<&str>, limiter: &Mutex<RateLimiter>, ledger: &Ledger, scrubber: &impl Scrubber) -> io::Result<DeliveryResult> {
    if body.trim().is_empty() || body.chars().count() > MAX_BODY_CHARS { return Err(invalid("invalid peer body")); }
    if !matches!(kind, "direct" | "broadcast") { return Err(invalid("invalid peer kind")); }
    if in_reply_to.is_some_and(|id|Uuid::parse_str(id).is_err()) { return Err(invalid("invalid peer reply reference")); }
    let peers = registry.scoped(sender.scope_key(), Some(&sender.session_id), scrubber, true)?;
    let mut targets = Vec::new();
    for id in recipients {
        let peer = peers.iter().find(|p| &p.session_id == id).ok_or_else(|| invalid("recipient is not a live scoped peer"))?;
        if !targets.iter().any(|p: &&PeerRecord| p.session_id == peer.session_id) { targets.push(peer); }
    }
    limiter.lock().map_err(|_| io::Error::other("peer rate limiter poisoned"))?
        .charge(turn_id, targets.len())?;
    let frame = PeerFrame { authenticated_pid: None, from_id: sender.session_id.clone(), from_title: sender.title.clone(), sent_at: now(), body: body.to_owned(),
        from_repo: Some(sender.scope_key().to_owned()), kind: (kind != "direct").then(|| kind.to_owned()) }.scrub(scrubber);
    let mut result = DeliveryResult { delivered: Vec::new(), failed: Vec::new(), record: None, ledger_error: None };
    for peer in targets {
        let path = Path::new(&peer.socket_path);
        if path.parent() == Some(registry.runtime()) && send(path, &frame).is_ok() { result.delivered.push(peer.session_id.clone()); }
        else { result.failed.push(peer.session_id.clone()); }
    }
    if result.delivered.is_empty() { return Err(io::Error::new(io::ErrorKind::NotConnected, "nothing was delivered")); }
    let message = Message { v: 1, id: Uuid::new_v4().simple().to_string(), ts: now(),
        sender: Sender { session: sender.session_id.clone(), title: Some(sender.title.clone()), repo: Some(sender.scope_key().to_owned()), model: sender.model.clone(), engine: sender.engine.clone() },
        to: result.delivered.clone(), kind: kind.to_owned(), in_reply_to: in_reply_to.map(str::to_owned), body: body.to_owned(), body_sha256: String::new(), latency_ms: None,
        turn: TurnRef { id: turn_id.map(str::to_owned), state: if turn_id.is_some() { "running" } else { "idle" }.to_owned() } };
    match ledger.append(message, scrubber) { Ok(record) => result.record = Some(record), Err(e) => result.ledger_error = Some(e.to_string()) }
    Ok(result)
}

// SPDX-License-Identifier: AGPL-3.0-only
//! Local peer transport and append-only delivery evidence. The caller supplies a LORE scrubber.
use crate::{now, PeerRecord, Registry, Scrubber};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::VecDeque;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt};
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
    let mut cred = libc::ucred { pid: 0, uid: 0, gid: 0 };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    let rc = unsafe { libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED,
        (&mut cred as *mut libc::ucred).cast(), &mut len) };
    if rc != 0 { return Err(io::Error::last_os_error()); }
    if len as usize != std::mem::size_of::<libc::ucred>() || cred.uid != unsafe { libc::geteuid() } {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "peer UID differs"));
    }
    Ok(())
}
fn read_frame(stream: &mut UnixStream) -> io::Result<PeerFrame> {
    let deadline = Instant::now() + TIMEOUT;
    let mut bytes = Vec::new();
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() { return Err(io::Error::new(io::ErrorKind::TimedOut, "peer frame timed out")); }
        stream.set_read_timeout(Some(remaining))?;
        let mut buf = [0u8; 4096];
        let n = stream.read(&mut buf)?;
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
struct PendingFrame { stream: UnixStream, bytes: Vec<u8>, started: Instant }
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
            stream.set_nonblocking(true)?;
            *pending = Some(PendingFrame { stream, bytes: Vec::new(), started: Instant::now() });
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
                *pending = None;
                return match parse_frame(&bytes) {
                    Ok(frame) => Ok(Some(frame.scrub(scrubber))),
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
                Ok(frame) => return Ok(frame.scrub(scrubber)),
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
use std::os::unix::ffi::OsStrExt;

pub fn send(path: &Path, frame: &PeerFrame) -> io::Result<()> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.file_type().is_socket() || meta.uid() != unsafe { libc::geteuid() }
        || meta.permissions().mode() & 0o077 != 0 { return Err(invalid("unsafe peer socket")); }
    let mut bytes = serde_json::to_vec(frame).map_err(io::Error::other)?;
    bytes.push(b'\n');
    if bytes.len() > MAX_FRAME_BYTES { return Err(invalid("peer frame too large")); }
    let mut stream = UnixStream::connect(path)?;
    same_user(&stream)?;
    stream.set_write_timeout(Some(TIMEOUT))?;
    stream.write_all(&bytes)
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
        fs::create_dir_all(parent)?;
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
/// The single local outbound path: scoped discovery, charge, send, then append only successful recipients.
pub fn deliver(registry: &Registry, sender: &PeerRecord, recipients: &[String], body: &str, kind: &str,
    turn_id: Option<&str>, limiter: &mut RateLimiter, ledger: &Ledger, scrubber: &impl Scrubber) -> io::Result<DeliveryResult> {
    if body.trim().is_empty() || body.chars().count() > MAX_BODY_CHARS { return Err(invalid("invalid peer body")); }
    if !matches!(kind, "direct" | "broadcast") { return Err(invalid("invalid peer kind")); }
    let peers = registry.scoped(sender.scope_key(), Some(&sender.session_id), scrubber, true)?;
    let mut targets = Vec::new();
    for id in recipients {
        let peer = peers.iter().find(|p| &p.session_id == id).ok_or_else(|| invalid("recipient is not a live scoped peer"))?;
        if !targets.iter().any(|p: &&PeerRecord| p.session_id == peer.session_id) { targets.push(peer); }
    }
    limiter.charge(turn_id, targets.len())?;
    let frame = PeerFrame { from_id: sender.session_id.clone(), from_title: sender.title.clone(), sent_at: now(), body: body.to_owned(),
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
        to: result.delivered.clone(), kind: kind.to_owned(), in_reply_to: None, body: body.to_owned(), body_sha256: String::new(), latency_ms: None,
        turn: TurnRef { id: turn_id.map(str::to_owned), state: if turn_id.is_some() { "running" } else { "idle" }.to_owned() } };
    match ledger.append(message, scrubber) { Ok(record) => result.record = Some(record), Err(e) => result.ledger_error = Some(e.to_string()) }
    Ok(result)
}

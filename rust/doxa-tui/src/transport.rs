//! Bounded line-JSON client for the DOXA session daemon.

use serde_json::{json, Map, Value};
use doxa_protocol::{Direction, WireError};
use std::collections::VecDeque;
use std::fs::OpenOptions;
use std::io::{self, BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::net::UnixStream;
use std::os::fd::AsRawFd;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;
use std::time::{Duration, Instant};

pub const PROTOCOL_NAME: &str = "doxa-daemon";
pub use doxa_protocol::{MAX_FRAME_BYTES, PROTOCOL_VERSION};
const MAX_QUEUED_FRAMES: usize = 1024;
// Darwin may report a queued AF_UNIX connection as established before the
// listener accepts it. A short hello deadline keeps a full backlog bounded.
#[cfg(target_os = "macos")]
const HELLO_TIMEOUT: Duration = Duration::from_millis(250);
#[cfg(not(target_os = "macos"))]
const HELLO_TIMEOUT: Duration = Duration::from_secs(10);
const CONNECT_TIMEOUT: Duration = Duration::from_millis(100);
const REPLY_TIMEOUT: Duration = Duration::from_secs(15);
const MAX_RESTORE_BYTES: u64 = 8 * 1024 * 1024;

/// A bounded tail ending at the daemon's persisted transcript size at hello time.
#[derive(Default)]
pub struct TranscriptSnapshot {
    pub bytes: Vec<u8>,
    pub earlier_bytes_omitted: bool,
    /// Present for a live-daemon restore. Offline archives do not have a
    /// trusted daemon identity and therefore cannot restore peer cards.
    pub session_id: Option<String>,
    pub peer_scope: Option<String>,
    pub peer_bytes: Vec<u8>,
}

#[derive(Debug)]
pub enum TransportError {
    Io(io::Error),
    Timeout,
    Closed,
    FrameTooLarge,
    Malformed(&'static str),
    ProtocolVersion(u64),
    QueueFull,
    RequestIdsExhausted,
}

impl std::fmt::Display for TransportError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(err) => write!(f, "daemon socket: {err}"),
            Self::Timeout => write!(f, "daemon response timed out"),
            Self::Closed => write!(f, "daemon closed the socket"),
            Self::FrameTooLarge => write!(f, "daemon frame exceeds 64 KiB"),
            Self::Malformed(why) => write!(f, "malformed daemon frame: {why}"),
            Self::ProtocolVersion(version) => write!(f, "unsupported daemon protocol v{version}"),
            Self::QueueFull => write!(f, "too many frames while awaiting daemon reply"),
            Self::RequestIdsExhausted => write!(f, "daemon request IDs exhausted"),
        }
    }
}

impl std::error::Error for TransportError {}

impl From<io::Error> for TransportError {
    fn from(err: io::Error) -> Self {
        match err.kind() {
            io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock => Self::Timeout,
            _ => Self::Io(err),
        }
    }
}

// A full AF_UNIX backlog can block a normal connect indefinitely. This
// descriptor is nonblocking and CLOEXEC from creation; EAGAIN means no
// connection was queued, so it must never be treated as a usable stream.
fn unix_connect_until(path: &Path, deadline: Instant) -> Result<UnixStream, TransportError> {
    let deadline = deadline.min(Instant::now() + CONNECT_TIMEOUT);
    if Instant::now() >= deadline { return Err(TransportError::Timeout); }
    let bytes = path.as_os_str().as_bytes();
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    if bytes.is_empty() || bytes.contains(&0) || bytes.len() >= address.sun_path.len() { return Err(TransportError::Malformed("invalid Unix socket path")); }
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (out, byte) in address.sun_path.iter_mut().zip(bytes) { *out = *byte as libc::c_char; }
    let stream = UnixStream::from(doxa_peers::credentials::nonblocking_unix_socket()?);
    let fd = stream.as_raw_fd();
    let length = (std::mem::offset_of!(libc::sockaddr_un, sun_path) + bytes.len() + 1) as libc::socklen_t;
    #[cfg(target_os = "macos")]
    { address.sun_len = length as u8; }
    if unsafe { libc::connect(fd, (&address as *const libc::sockaddr_un).cast(), length) } != 0 {
        let error = io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::EINPROGRESS) { return Err(error.into()); }
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() { return Err(TransportError::Timeout); }
            let mut poll = libc::pollfd { fd, events:libc::POLLOUT, revents:0 };
            let milliseconds = remaining.as_millis().saturating_add(1).min(i32::MAX as u128) as i32;
            let result = unsafe { libc::poll(&mut poll, 1, milliseconds) };
            if result == 0 { return Err(TransportError::Timeout); }
            if result < 0 {
                let error = io::Error::last_os_error();
                if error.kind() == io::ErrorKind::Interrupted { continue; }
                return Err(error.into());
            }
            if let Some(error) = stream.take_error()? { return Err(error.into()); }
            stream.peer_addr()?; // A writable unconnected descriptor is not success.
            break;
        }
    }
    stream.set_nonblocking(false)?;
    Ok(stream)
}

/// A synchronous client. `hello` and every frame returned by `next_frame`
/// remain JSON objects so the UI can consume new daemon fields without
/// changing transport types.
pub struct DaemonClient {
    reader: BufReader<UnixStream>,
    writer: UnixStream,
    pub hello: Value,
    /// The next sequence to use when reconnecting.
    pub cursor: u64,
    next_id: u64,
    queued: VecDeque<Value>,
    pending_bytes: Vec<u8>,
}

impl DaemonClient {
    /// `None` replays the daemon's entire retained event ring.
    pub fn connect(path: impl AsRef<Path>, cursor: Option<u64>) -> Result<Self, TransportError> {
        Self::connect_inner(path, cursor, false).map(|(client, _)| client)
    }

    /// Attach after the sampled hello sequence. Used only after a verified
    /// idle fleet checkpoint so old completion events cannot run twice.
    pub(crate) fn connect_tail(path:&Path)->Result<Self,TransportError>{
        let deadline=Instant::now()+HELLO_TIMEOUT;let stream=unix_connect_until(path,deadline)?;
        Self::handshake(stream,None,false,true,Some(deadline)).map(|(client,_)|client)
    }
    pub(crate) fn has_buffered_frames(&self)->bool{
        !self.queued.is_empty()||!self.pending_bytes.is_empty()||!self.reader.buffer().is_empty()
    }

    /// Deadline bounds socket connect, hello and attach together. Used by
    /// fleet teardown so a blocked listener cannot escape the shared deadline.
    pub(crate) fn connect_until(path: &Path, cursor: Option<u64>, deadline: Instant) -> Result<Self, TransportError> {
        let deadline = deadline.min(Instant::now() + HELLO_TIMEOUT);
        let stream = unix_connect_until(path, deadline)?;
        Self::handshake(stream, cursor, false, false, Some(deadline)).map(|(client, _)| client)
    }

    /// Restore the durable local transcript before attaching at the hello
    /// sequence. Old daemons and unreadable files fall back to ring replay.
    pub fn connect_for_restore(path: impl AsRef<Path>) -> Result<(Self, Option<TranscriptSnapshot>), TransportError> {
        Self::connect_inner(path, None, true)
    }

    fn connect_inner(path: impl AsRef<Path>, cursor: Option<u64>, restore: bool) -> Result<(Self, Option<TranscriptSnapshot>), TransportError> {
        let deadline = Instant::now() + HELLO_TIMEOUT;
        let stream = unix_connect_until(path.as_ref(), deadline)?;
        Self::handshake(stream, cursor, restore, false, Some(deadline))
    }
    fn handshake(stream: UnixStream, cursor: Option<u64>, restore: bool, tail: bool, deadline: Option<Instant>) -> Result<(Self, Option<TranscriptSnapshot>), TransportError> {
        let remaining = || -> Result<Duration, TransportError> {
            deadline.map(|at| at.checked_duration_since(Instant::now()).filter(|time| !time.is_zero()).ok_or(TransportError::Timeout)).unwrap_or(Ok(HELLO_TIMEOUT))
        };
        let writer = stream.try_clone()?;
        writer.set_write_timeout(Some(if deadline.is_some() { remaining()? } else { REPLY_TIMEOUT }))?;
        let mut reader = BufReader::new(stream);
        let mut pending_bytes = Vec::new();
        let hello = read_json_until(&mut reader, &mut pending_bytes, deadline)?;
        validate_hello(&hello)?;
        let snapshot = if restore { read_snapshot(&hello) } else { None };
        // The host samples this cursor before reading either durable file.
        // Events published during the bounded file reads must replay afterward.
        let attach_cursor = if tail {
            Some(hello["next_seq"].as_u64().ok_or(TransportError::Malformed("daemon tail cursor missing"))?)
        } else if snapshot.is_some() {
            hello["transcript_seq"].as_u64().or_else(|| hello["next_seq"].as_u64())
        } else { cursor };
        let mut client = Self {
            reader, writer, hello, cursor: attach_cursor.unwrap_or(0), next_id: 1,
            queued: VecDeque::new(),
            pending_bytes,
        };
        if deadline.is_some() { client.writer.set_write_timeout(Some(remaining()?))?; }
        client.write_json(&json!({"type": "attach", "cursor": attach_cursor}))?;
        client.writer.set_write_timeout(Some(REPLY_TIMEOUT))?;
        Ok((client, snapshot))
    }

    /// Read one validated event or reply JSON object. An idle event stream
    /// blocks until a frame arrives or the daemon closes the socket.
    pub fn next_frame(&mut self) -> Result<Value, TransportError> {
        if let Some(frame) = self.queued.pop_front() {
            return Ok(frame);
        }
        self.read_frame()
    }

    /// Return `None` if no full frame arrives before `timeout`. Bytes from a
    /// partial line are retained for the next poll.
    pub fn poll_frame(&mut self, timeout: Duration) -> Result<Option<Value>, TransportError> {
        if let Some(frame) = self.queued.pop_front() {
            return Ok(Some(frame));
        }
        let result = self.read_frame_until(Some(Instant::now() + timeout));
        match result {
            Err(TransportError::Timeout) => Ok(None),
            other => other.map(Some),
        }
    }

    /// Send a prompt and await its matching acknowledgement. Intervening
    /// event frames are preserved for `next_frame`.
    pub fn prompt(&mut self, text: &str) -> Result<Value, TransportError> {
        let id = self.request_id()?;
        self.write_json(&json!({"type": "prompt", "id": id, "text": text}))?;
        self.await_reply(id)
    }

    pub fn call(&mut self, method: &str, params: Map<String, Value>) -> Result<Value, TransportError> {
        if method.is_empty() {
            return Err(TransportError::Malformed("empty call method"));
        }
        let id = self.request_id()?;
        self.write_json(&json!({"type": "call", "id": id, "method": method, "params": params}))?;
        self.await_reply(id)
    }

    /// Bind destructive requests to the process owning this connected socket.
    pub(crate) fn verify_peer(&self, expected_pid: i32) -> Result<(), TransportError> {
        #[cfg(target_os = "macos")]
        {
            let _ = expected_pid;
            if doxa_peers::credentials::peer_uid(&self.writer)? != unsafe { libc::geteuid() } {
                return Err(TransportError::Malformed("daemon peer owner changed"));
            }
        }
        #[cfg(not(target_os = "macos"))]
        {
        let credentials = doxa_peers::credentials::peer_credentials(&self.writer)?;
        if credentials.uid != unsafe { libc::geteuid() } || credentials.pid != expected_pid {
            return Err(TransportError::Malformed("daemon peer process changed"));
        }
        }
        Ok(())
    }

    pub(crate) fn call_until(&mut self, method: &str, params: Map<String, Value>, deadline: Instant) -> Result<Value, TransportError> {
        let remaining = deadline.checked_duration_since(Instant::now()).ok_or(TransportError::Timeout)?;
        self.writer.set_write_timeout(Some(remaining))?;
        let id = self.request_id()?;
        self.write_json(&json!({"type":"call","id":id,"method":method,"params":params}))?;
        self.await_reply_until(id, deadline)
    }

    fn request_id(&mut self) -> Result<u64, TransportError> {
        let id = self.next_id;
        self.next_id = id.checked_add(1).ok_or(TransportError::RequestIdsExhausted)?;
        Ok(id)
    }

    fn write_json(&mut self, frame: &Value) -> Result<(), TransportError> {
        let bytes = doxa_protocol::encode_line(frame, Direction::ClientToServer).map_err(map_wire_error)?;
        self.writer.write_all(&bytes)?;
        Ok(())
    }

    fn read_frame(&mut self) -> Result<Value, TransportError> { self.read_frame_until(None) }
    fn read_frame_until(&mut self, deadline: Option<Instant>) -> Result<Value, TransportError> {
        let frame = read_json_until(&mut self.reader, &mut self.pending_bytes, deadline)?;
        validate_frame(&frame)?;
        if frame["type"] == "event" {
            self.cursor = frame["seq"].as_u64().unwrap().checked_add(1)
                .ok_or(TransportError::Malformed("event sequence overflow"))?;
        }
        Ok(frame)
    }

    fn await_reply(&mut self, id: u64) -> Result<Value, TransportError> {
        if let Some(index) = self.queued.iter().position(|f| f["type"] == "reply" && f["id"] == id) {
            return Ok(self.queued.remove(index).unwrap());
        }
        self.await_reply_until(id, Instant::now() + REPLY_TIMEOUT)
    }

    fn await_reply_until(&mut self, id: u64, deadline: Instant) -> Result<Value, TransportError> {
        let result = (|| loop {
            let frame = self.read_frame_until(Some(deadline))?;
            if frame["type"] == "reply" && frame["id"] == id {
                return Ok(frame);
            }
            if self.queued.len() == MAX_QUEUED_FRAMES {
                return Err(TransportError::QueueFull);
            }
            self.queued.push_back(frame);
        })();
        result
    }
}

fn read_snapshot(hello: &Value) -> Option<TranscriptSnapshot> {
    const MAX_PEER_RESTORE_BYTES: u64 = 4 * 1024 * 1024;
    let session_id = hello["session_id"].as_str().filter(|id| doxa_transcript::valid_session_id(id))?.to_owned();
    let (bytes, earlier_bytes_omitted) = if let (Some(path), Some(size)) =
        (hello["transcript_path"].as_str(), hello["transcript_bytes"].as_u64()) {
        if size == 0 { (Vec::new(), false) } else {
            let restored = (|| -> Option<(Vec<u8>, bool)> {
                // Never let a FIFO block startup or follow a final symlink.
                let mut file = OpenOptions::new().read(true)
                    .custom_flags(libc::O_NONBLOCK | libc::O_NOFOLLOW).open(path).ok()?;
                let meta = file.metadata().ok()?;
                if !meta.is_file() || meta.uid() != unsafe { libc::geteuid() } || meta.len() < size { return None; }
                let start = size.saturating_sub(MAX_RESTORE_BYTES);
                file.seek(SeekFrom::Start(start)).ok()?;
                let mut bytes = vec![0; (size - start) as usize];
                file.read_exact(&mut bytes).ok()?;
                if start > 0 {
                    let after_first_line = bytes.iter().position(|byte| *byte == b'\n')? + 1;
                    bytes.drain(..after_first_line);
                }
                Some((bytes, start > 0))
            })();
            restored.unwrap_or_default()
        }
    } else { (Vec::new(), false) };
    let (peer_bytes, peer_scope) = if let (Some(path), Some(size), Some(scope)) = (
        hello["peer_history_path"].as_str(), hello["peer_history_bytes"].as_u64(), hello["peer_history_scope"].as_str()) {
        if size > 0 && size <= MAX_PEER_RESTORE_BYTES && !scope.is_empty() && scope.len() <= 512
            && !scope.chars().any(char::is_control) {
            let restored = (|| -> Option<Vec<u8>> {
                let mut file = OpenOptions::new().read(true)
                    .custom_flags(libc::O_NONBLOCK | libc::O_NOFOLLOW).open(path).ok()?;
                let meta = file.metadata().ok()?;
                // A concurrent append is fine: the hello byte count is the
                // snapshot boundary. Symlinks, hardlinks, public files, and
                // files shorter than that boundary are rejected.
                if !meta.is_file() || meta.uid() != unsafe { libc::geteuid() } || meta.nlink() != 1
                    || meta.mode() & 0o077 != 0 || meta.len() < size { return None; }
                let mut bytes = vec![0; size as usize];
                file.read_exact(&mut bytes).ok()?;
                Some(bytes)
            })();
            restored.map(|bytes| (bytes, Some(scope.to_owned()))).unwrap_or_default()
        } else { (Vec::new(), None) }
    } else { (Vec::new(), None) };
    if bytes.is_empty() && peer_bytes.is_empty() { return None; }
    Some(TranscriptSnapshot { bytes, earlier_bytes_omitted, session_id:Some(session_id), peer_scope, peer_bytes })
}

fn read_json_until(reader: &mut BufReader<UnixStream>, pending: &mut Vec<u8>, deadline: Option<Instant>) -> Result<Value, TransportError> {
    loop {
        if reader.buffer().is_empty() {
            let millis = match deadline {
                Some(deadline) => deadline.checked_duration_since(Instant::now())
                    .filter(|remaining| !remaining.is_zero())
                    .map(|remaining| remaining.as_millis().max(1).min(i32::MAX as u128) as i32)
                    .ok_or(TransportError::Timeout)?,
                None => -1,
            };
            let mut ready = libc::pollfd { fd: reader.get_ref().as_raw_fd(), events: libc::POLLIN, revents: 0 };
            let result = unsafe { libc::poll(&mut ready, 1, millis) };
            if result == 0 { return Err(TransportError::Timeout); }
            if result < 0 {
                let error = io::Error::last_os_error();
                if error.kind() == io::ErrorKind::Interrupted { continue; }
                return Err(error.into());
            }
        }
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return if pending.is_empty() { Err(TransportError::Closed) }
                else { Err(TransportError::Malformed("unterminated frame")) };
        }
        let end = available.iter().position(|byte| *byte == b'\n');
        let amount = end.map_or(available.len(), |position| position + 1);
        if pending.len() + amount > MAX_FRAME_BYTES {
            return Err(TransportError::FrameTooLarge);
        }
        pending.extend_from_slice(&available[..amount]);
        reader.consume(amount);
        if end.is_some() {
            let parsed = serde_json::from_slice(pending).map_err(|_| TransportError::Malformed("invalid JSON"));
            pending.clear();
            return parsed;
        }
    }
}

fn validate_hello(frame: &Value) -> Result<(), TransportError> {
    if frame["type"] != "hello" {
        return Err(TransportError::Malformed("expected hello"));
    }
    doxa_protocol::validate(frame, Direction::ServerToClient).map_err(map_wire_error)
}

fn validate_frame(frame: &Value) -> Result<(), TransportError> {
    if frame["type"] == "hello" { return Err(TransportError::Malformed("unexpected hello")); }
    doxa_protocol::validate(frame, Direction::ServerToClient).map_err(map_wire_error)
}

fn map_wire_error(error: WireError) -> TransportError {
    match error {
        WireError::FrameTooLarge => TransportError::FrameTooLarge,
        WireError::UnsupportedVersion(version) => TransportError::ProtocolVersion(version),
        WireError::InvalidField(field) => TransportError::Malformed(field),
        WireError::InvalidJson => TransportError::Malformed("invalid JSON"),
        WireError::IncompleteFrame => TransportError::Malformed("unterminated frame"),
        WireError::UnknownType => TransportError::Malformed("unknown frame type"),
    }
}

#[cfg(test)]
mod deadline_tests {
    use super::*;
    use std::os::unix::net::UnixListener;

    #[test]
    fn tail_connection_uses_hello_cursor_and_keeps_new_events(){
        use std::os::unix::net::UnixListener;
        let dir=tempfile::tempdir().unwrap();let socket=dir.path().join("tail.sock");let listener=UnixListener::bind(&socket).unwrap();
        let server=std::thread::spawn(move||{
            let (mut stream,_)=listener.accept().unwrap();
            writeln!(stream,"{}",json!({"type":"hello","proto":1,"session_id":"tail","engine":"fixture","cwd":"/repo","next_seq":7})).unwrap();
            let mut reader=BufReader::new(stream.try_clone().unwrap());let mut line=String::new();reader.read_line(&mut line).unwrap();
            let attach:Value=serde_json::from_str(&line).unwrap();assert_eq!(attach["cursor"],7);
            writeln!(stream,"{}",json!({"type":"event","seq":7,"event":{"type":"turn_done","data":{}}})).unwrap();
        });
        let mut client=DaemonClient::connect_tail(&socket).unwrap();assert_eq!(client.cursor,7);
        assert_eq!(client.next_frame().unwrap()["seq"],7);assert_eq!(client.cursor,8);server.join().unwrap();
    }
    #[test]
    fn peer_restore_reads_only_the_private_hello_bound_snapshot() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let mut permissions = std::fs::metadata(dir.path()).unwrap().permissions();
        permissions.set_mode(0o700);
        std::fs::set_permissions(dir.path(), permissions).unwrap();
        let transcript_path = dir.path().join("transcript.jsonl");
        let journal_path = dir.path().join("peer-owner-session.jsonl");
        std::fs::write(&transcript_path, b"transcript\n").unwrap();
        std::fs::write(&journal_path, b"{\"old\":true}\n").unwrap();
        for path in [&transcript_path, &journal_path] {
            let mut permissions = std::fs::metadata(path).unwrap().permissions();
            permissions.set_mode(0o600);
            std::fs::set_permissions(path, permissions).unwrap();
        }
        let peer_snapshot_size = std::fs::metadata(&journal_path).unwrap().len();
        std::fs::OpenOptions::new().append(true).open(&journal_path).unwrap()
            .write_all(b"{\"later\":true}\n").unwrap();
        let hello = json!({"session_id":"owner-session","transcript_path":transcript_path,
            "transcript_bytes":11,"peer_history_path":journal_path,"peer_history_bytes":peer_snapshot_size,
            "peer_history_scope":"/private/project"});
        let snapshot = read_snapshot(&hello).unwrap();
        assert_eq!(snapshot.session_id.as_deref(), Some("owner-session"));
        assert_eq!(snapshot.peer_scope.as_deref(), Some("/private/project"));
        assert_eq!(snapshot.peer_bytes, b"{\"old\":true}\n");
        assert_eq!(snapshot.bytes, b"transcript\n");

        let mut permissions = std::fs::metadata(&journal_path).unwrap().permissions();
        permissions.set_mode(0o644);
        std::fs::set_permissions(&journal_path, permissions).unwrap();
        let fallback = read_snapshot(&hello).unwrap();
        assert!(fallback.peer_bytes.is_empty(), "an unsafe sidecar is discarded");
        assert_eq!(fallback.bytes, b"transcript\n", "the independent transcript remains restorable");
    }

    #[test]
    fn ordinary_and_restore_connections_reject_full_backlog_without_blocking() {
        for restore in [false, true] {
            let dir = tempfile::tempdir().unwrap(); let path = dir.path().join("backlog.sock");
            let listener = UnixListener::bind(&path).unwrap();
            assert_eq!(unsafe { libc::listen(listener.as_raw_fd(), 0) }, 0);
            let held = UnixStream::connect(&path).unwrap();
            // Keep the backlog full until the connection attempt has returned.
            // CI scheduling can delay either thread beyond the hello timeout;
            // a timed listener close plus elapsed-time assertion cannot prove
            // which event released the client. The channel proves ordering.
            let (completed, completion) = std::sync::mpsc::channel();
            let release = std::thread::spawn(move || {
                // Unblock a broken, blocking client so the test can fail and
                // join its threads. This is only a fixture watchdog; production
                // connect/hello deadlines retain their original values.
                let forced = completion.recv_timeout(Duration::from_secs(2)).is_err();
                drop(listener); drop(held);
                forced
            });
            let error = if restore { DaemonClient::connect_for_restore(&path).err() }
                else { DaemonClient::connect(&path, None).err() };
            let _ = completed.send(());
            assert!(!release.join().unwrap(), "connect waited for the fixture watchdog to release the backlog");
            assert!(matches!(&error, Some(TransportError::Timeout))
                || cfg!(target_os = "macos") && matches!(&error, Some(TransportError::Io(error))
                    if error.kind() == io::ErrorKind::ConnectionRefused),
                "expected a full-backlog refusal while the listener was held: {error:?}");
        }
    }
    #[test]
    fn full_listener_backlog_never_becomes_a_false_connected_stream() {
        let dir = tempfile::tempdir().unwrap(); let path = dir.path().join("backlog.sock");
        let listener = UnixListener::bind(&path).unwrap();
        assert_eq!(unsafe { libc::listen(listener.as_raw_fd(), 1) }, 0);
        let mut connected = Vec::new(); let started = Instant::now(); let mut full = false;
        for _ in 0..16 {
            match unix_connect_until(&path, Instant::now() + Duration::from_millis(100)) {
                Ok(stream) => {
                    assert!(stream.peer_addr().is_ok());
                    assert_ne!(unsafe { libc::fcntl(stream.as_raw_fd(), libc::F_GETFD) } & libc::FD_CLOEXEC, 0);
                    connected.push(stream);
                }
                Err(TransportError::Timeout) => { full = true; break; }
                #[cfg(target_os = "macos")]
                Err(TransportError::Io(error)) if error.kind() == io::ErrorKind::ConnectionRefused => { full = true; break; }
                Err(error) => panic!("unexpected backlog error: {error}"),
            }
        }
        assert!(full, "listener backlog was not filled");
        assert!(started.elapsed() < Duration::from_secs(1));
        let result = DaemonClient::connect_until(&path, None, Instant::now() + Duration::from_millis(100));
        assert!(matches!(&result, Err(TransportError::Timeout))
            || cfg!(target_os = "macos") && matches!(&result, Err(TransportError::Io(error)) if error.kind() == io::ErrorKind::ConnectionRefused));
    }
    #[test]
    fn partial_hello_cannot_reset_the_connection_deadline() {
        let dir = tempfile::tempdir().unwrap(); let path = dir.path().join("trickle.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            for _ in 0..20 {
                if stream.write_all(b" ").is_err() { break; }
                std::thread::sleep(Duration::from_millis(50));
            }
        });
        let started = Instant::now();
        assert!(matches!(DaemonClient::connect_until(&path, None, started + Duration::from_millis(200)), Err(TransportError::Timeout)));
        assert!(started.elapsed() < Duration::from_millis(800));
        server.join().unwrap();
    }

}

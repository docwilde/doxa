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
const HELLO_TIMEOUT: Duration = Duration::from_secs(10);
const REPLY_TIMEOUT: Duration = Duration::from_secs(15);
const MAX_RESTORE_BYTES: u64 = 8 * 1024 * 1024;

/// A bounded tail ending at the daemon's persisted transcript size at hello time.
pub struct TranscriptSnapshot {
    pub bytes: Vec<u8>,
    pub earlier_bytes_omitted: bool,
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

    /// Deadline bounds socket connect, hello and attach together. Used by
    /// fleet teardown so a blocked listener cannot escape the shared deadline.
    pub(crate) fn connect_until(path: &Path, cursor: Option<u64>, deadline: Instant) -> Result<Self, TransportError> {
        let deadline = deadline.min(Instant::now() + HELLO_TIMEOUT);
        let stream = unix_connect_until(path, deadline)?;
        Self::handshake(stream, cursor, false, Some(deadline)).map(|(client, _)| client)
    }

    /// Restore the durable local transcript before attaching at the hello
    /// sequence. Old daemons and unreadable files fall back to ring replay.
    pub fn connect_for_restore(path: impl AsRef<Path>) -> Result<(Self, Option<TranscriptSnapshot>), TransportError> {
        Self::connect_inner(path, None, true)
    }

    fn connect_inner(path: impl AsRef<Path>, cursor: Option<u64>, restore: bool) -> Result<(Self, Option<TranscriptSnapshot>), TransportError> {
        let deadline = Instant::now() + HELLO_TIMEOUT;
        let stream = unix_connect_until(path.as_ref(), deadline)?;
        Self::handshake(stream, cursor, restore, Some(deadline))
    }
    fn handshake(stream: UnixStream, cursor: Option<u64>, restore: bool, deadline: Option<Instant>) -> Result<(Self, Option<TranscriptSnapshot>), TransportError> {
        let remaining = || -> Result<Duration, TransportError> {
            deadline.map(|at| at.checked_duration_since(Instant::now()).filter(|time| !time.is_zero()).ok_or(TransportError::Timeout)).unwrap_or(Ok(HELLO_TIMEOUT))
        };
        let writer = stream.try_clone()?;
        writer.set_write_timeout(Some(if deadline.is_some() { remaining()? } else { REPLY_TIMEOUT }))?;
        let mut reader = BufReader::new(stream);
        reader.get_ref().set_read_timeout(Some(remaining()?))?;
        let mut pending_bytes = Vec::new();
        let hello = read_json_until(&mut reader, &mut pending_bytes, deadline)?;
        validate_hello(&hello)?;
        reader.get_ref().set_read_timeout(None)?;
        let snapshot = if restore { read_snapshot(&hello) } else { None };
        let attach_cursor = if snapshot.is_some() { hello["next_seq"].as_u64() } else { cursor };
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
        self.reader.get_ref().set_read_timeout(Some(timeout))?;
        let result = self.read_frame_until(Some(Instant::now() + timeout));
        self.reader.get_ref().set_read_timeout(None)?;
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
            let remaining = deadline.checked_duration_since(Instant::now()).ok_or(TransportError::Timeout)?;
            self.reader.get_ref().set_read_timeout(Some(remaining))?;
            let frame = self.read_frame_until(Some(deadline))?;
            if frame["type"] == "reply" && frame["id"] == id {
                return Ok(frame);
            }
            if self.queued.len() == MAX_QUEUED_FRAMES {
                return Err(TransportError::QueueFull);
            }
            self.queued.push_back(frame);
        })();
        self.reader.get_ref().set_read_timeout(None)?;
        result
    }
}

fn read_snapshot(hello: &Value) -> Option<TranscriptSnapshot> {
    let path = hello["transcript_path"].as_str()?;
    let size = hello["transcript_bytes"].as_u64()?;
    if size == 0 { return None; }
    // Hello comes from the socket peer. Never let a FIFO in its claimed
    // transcript path block startup, or follow a final symlink to another file.
    let mut file = OpenOptions::new().read(true)
        .custom_flags(libc::O_NONBLOCK | libc::O_NOFOLLOW)
        .open(path).ok()?;
    let meta = file.metadata().ok()?;
    if !meta.is_file() || meta.uid() != unsafe { libc::geteuid() } || meta.len() < size {
        return None;
    }
    let start = size.saturating_sub(MAX_RESTORE_BYTES);
    file.seek(SeekFrom::Start(start)).ok()?;
    let mut bytes = vec![0; (size - start) as usize];
    file.read_exact(&mut bytes).ok()?;
    if start > 0 {
        let after_first_line = bytes.iter().position(|byte| *byte == b'\n')? + 1;
        bytes.drain(..after_first_line);
    }
    Some(TranscriptSnapshot { bytes, earlier_bytes_omitted: start > 0 })
}

fn read_json_until(reader: &mut BufReader<UnixStream>, pending: &mut Vec<u8>, deadline: Option<Instant>) -> Result<Value, TransportError> {
    loop {
        if let Some(deadline) = deadline {
            let remaining = deadline.checked_duration_since(Instant::now()).filter(|time| !time.is_zero()).ok_or(TransportError::Timeout)?;
            reader.get_ref().set_read_timeout(Some(remaining))?;
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
    fn ordinary_and_restore_connections_reject_full_backlog_without_blocking() {
        for restore in [false, true] {
            let dir = tempfile::tempdir().unwrap(); let path = dir.path().join("backlog.sock");
            let listener = UnixListener::bind(&path).unwrap();
            assert_eq!(unsafe { libc::listen(listener.as_raw_fd(), 0) }, 0);
            let held = UnixStream::connect(&path).unwrap();
            // Bound a regression too: closing the listener releases an old
            // blocking connect, rather than leaving the test runner hung.
            let release = std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(300)); drop(listener); drop(held);
            });
            let started = Instant::now();
            let failed = if restore { DaemonClient::connect_for_restore(&path).is_err() }
                else { DaemonClient::connect(&path, None).is_err() };
            let elapsed = started.elapsed(); release.join().unwrap();
            assert!(failed);
            assert!(elapsed < Duration::from_millis(150), "connect waited for backlog release: {elapsed:?}");
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
                Err(error) => panic!("unexpected backlog error: {error}"),
            }
        }
        assert!(full, "listener backlog was not filled");
        assert!(started.elapsed() < Duration::from_secs(1));
        assert!(matches!(DaemonClient::connect_until(&path, None, Instant::now() + Duration::from_millis(100)), Err(TransportError::Timeout)));
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

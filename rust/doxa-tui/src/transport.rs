//! Bounded line-JSON client for the DOXA session daemon.

use serde_json::{json, Map, Value};
use std::collections::VecDeque;
use std::fs::OpenOptions;
use std::io::{self, BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::{Duration, Instant};

pub const PROTOCOL_NAME: &str = "doxa-daemon";
pub const PROTOCOL_VERSION: u64 = 1;
pub const MAX_FRAME_BYTES: usize = 64 * 1024;
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

    /// Restore the durable local transcript before attaching at the hello
    /// sequence. Old daemons and unreadable files fall back to ring replay.
    pub fn connect_for_restore(path: impl AsRef<Path>) -> Result<(Self, Option<TranscriptSnapshot>), TransportError> {
        Self::connect_inner(path, None, true)
    }

    fn connect_inner(path: impl AsRef<Path>, cursor: Option<u64>, restore: bool) -> Result<(Self, Option<TranscriptSnapshot>), TransportError> {
        let stream = UnixStream::connect(path)?;
        let writer = stream.try_clone()?;
        writer.set_write_timeout(Some(REPLY_TIMEOUT))?;
        let mut reader = BufReader::new(stream);
        reader.get_ref().set_read_timeout(Some(HELLO_TIMEOUT))?;
        let mut pending_bytes = Vec::new();
        let hello = read_json(&mut reader, &mut pending_bytes)?;
        validate_hello(&hello)?;
        reader.get_ref().set_read_timeout(None)?;
        let snapshot = if restore { read_snapshot(&hello) } else { None };
        let attach_cursor = if snapshot.is_some() { hello["next_seq"].as_u64() } else { cursor };
        let mut client = Self {
            reader, writer, hello, cursor: attach_cursor.unwrap_or(0), next_id: 1,
            queued: VecDeque::new(),
            pending_bytes,
        };
        client.write_json(&json!({"type": "attach", "cursor": attach_cursor}))?;
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
        let result = self.read_frame();
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

    fn request_id(&mut self) -> Result<u64, TransportError> {
        let id = self.next_id;
        self.next_id = id.checked_add(1).ok_or(TransportError::RequestIdsExhausted)?;
        Ok(id)
    }

    fn write_json(&mut self, frame: &Value) -> Result<(), TransportError> {
        let mut bytes = serde_json::to_vec(frame).map_err(|_| TransportError::Malformed("cannot encode request"))?;
        bytes.push(b'\n');
        if bytes.len() > MAX_FRAME_BYTES {
            return Err(TransportError::FrameTooLarge);
        }
        self.writer.write_all(&bytes)?;
        Ok(())
    }

    fn read_frame(&mut self) -> Result<Value, TransportError> {
        let frame = read_json(&mut self.reader, &mut self.pending_bytes)?;
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
        let deadline = Instant::now() + REPLY_TIMEOUT;
        let result = (|| loop {
            let remaining = deadline.checked_duration_since(Instant::now()).ok_or(TransportError::Timeout)?;
            self.reader.get_ref().set_read_timeout(Some(remaining))?;
            let frame = self.read_frame()?;
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

fn read_json(reader: &mut BufReader<UnixStream>, pending: &mut Vec<u8>) -> Result<Value, TransportError> {
    loop {
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
    let version = frame["proto"].as_u64().ok_or(TransportError::Malformed("missing protocol version"))?;
    if version != PROTOCOL_VERSION {
        return Err(TransportError::ProtocolVersion(version));
    }
    nonempty_string(frame, "session_id")?;
    nonempty_string(frame, "cwd")?;
    frame["next_seq"].as_u64().ok_or(TransportError::Malformed("missing next_seq"))?;
    for field in ["model", "engine"] {
        if !frame[field].is_null() && frame[field].as_str().is_none() {
            return Err(TransportError::Malformed("invalid session field"));
        }
    }
    Ok(())
}

fn validate_frame(frame: &Value) -> Result<(), TransportError> {
    match frame["type"].as_str() {
        Some("event") => {
            frame["seq"].as_u64().ok_or(TransportError::Malformed("missing event seq"))?;
            if !frame["turn"].is_null() && frame["turn"].as_str().is_none() {
                return Err(TransportError::Malformed("invalid event turn"));
            }
            nonempty_string(&frame["event"], "type")?;
            frame["event"]["data"].as_object().ok_or(TransportError::Malformed("missing event data"))?;
        }
        Some("reply") => {
            frame["id"].as_u64().ok_or(TransportError::Malformed("missing reply id"))?;
            frame["ok"].as_bool().ok_or(TransportError::Malformed("missing reply ok"))?;
        }
        _ => return Err(TransportError::Malformed("unknown frame type")),
    }
    Ok(())
}

fn nonempty_string<'a>(frame: &'a Value, key: &'static str) -> Result<&'a str, TransportError> {
    frame[key].as_str().filter(|s| !s.is_empty()).ok_or(TransportError::Malformed(key))
}

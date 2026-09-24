//! Bounded client for DOXA's external LORE sidecar.
//!
//! LORE itself remains authoritative for scrubbing and context. This crate
//! never opens LORE's SQLite database or stores memory. A failed sidecar
//! returns an error; callers must not silently persist unsanitized text.

use serde_json::{json, Value};
use std::collections::HashSet;
use std::io::{self, Read, Write};
#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

pub const PROTOCOL_VERSION: u64 = 1;
pub const MAX_FRAME_BYTES: usize = 1024 * 1024;

#[derive(Debug, Clone, PartialEq)]
pub struct SyncState {
    pub last_pull_age_s: Option<f64>,
    pub unpushed: u64,
    pub conflicts: u64,
    pub unverified: u64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ConsultHit {
    pub id: u64,
    pub claim: String,
    pub claim_truncated: bool,
    pub confidence: f64,
    pub score: f64,
}

#[derive(Debug)]
pub enum LoreError {
    Io(io::Error),
    Timeout,
    Closed,
    InvalidFrame,
    FrameTooLarge,
    Unavailable,
    Remote(&'static str),
}

impl std::fmt::Display for LoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(error) => write!(f, "LORE sidecar I/O: {error}"),
            Self::Timeout => write!(f, "LORE sidecar timed out"),
            Self::Closed => write!(f, "LORE sidecar closed"),
            Self::InvalidFrame => write!(f, "invalid LORE sidecar frame"),
            Self::FrameTooLarge => write!(f, "LORE sidecar frame too large"),
            Self::Unavailable => write!(f, "LORE is unavailable"),
            Self::Remote(code) => write!(f, "LORE operation failed: {code}"),
        }
    }
}

impl std::error::Error for LoreError {}

enum ReadResult {
    Line(Vec<u8>),
    Closed,
    TooLarge,
    Io,
}

pub struct LoreClient {
    child: Child,
    stdin: ChildStdin,
    rx: Receiver<ReadResult>,
    reader: Option<JoinHandle<()>>,
    timeout: Duration,
    next_id: u64,
    alive: bool,
    capabilities: HashSet<String>,
}

impl LoreClient {
    /// Launch the sidecar lazily, only when a session requests LORE.
    /// `python` should point at the environment that installed DOXA and LORE.
    pub fn spawn(python: &Path, timeout: Duration) -> Result<Self, LoreError> {
        let mut command = Command::new(python);
        command
            .args(["-m", "doxa.lore_bridge"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(unix)]
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() == -1 {
                    Err(io::Error::last_os_error())
                } else {
                    Ok(())
                }
            });
        }
        // An interpreter being replaced during an update can briefly return
        // ETXTBSY. Retry only that transient error; other spawn failures are
        // reported immediately.
        #[cfg(unix)]
        let mut busy_retries = 0;
        let mut child = loop {
            match command.spawn() {
                Ok(child) => break child,
                Err(error) => {
                    #[cfg(unix)]
                    if error.raw_os_error() == Some(libc::ETXTBSY) && busy_retries < 10 {
                        busy_retries += 1;
                        thread::sleep(Duration::from_millis(10));
                        continue;
                    }
                    return Err(LoreError::Io(error));
                }
            }
        };
        let stdin = child.stdin.take().ok_or(LoreError::InvalidFrame)?;
        #[cfg(unix)]
        {
            let fd = stdin.as_raw_fd();
            let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
            if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0
            {
                let error = io::Error::last_os_error();
                #[cfg(unix)]
                unsafe {
                    libc::kill(-(child.id() as i32), libc::SIGKILL);
                }
                let _ = child.wait();
                return Err(LoreError::Io(error));
            }
        }
        let stdout = child.stdout.take().ok_or(LoreError::InvalidFrame)?;
        let (tx, rx) = mpsc::channel();
        let reader = thread::spawn(move || read_frames(stdout, tx));
        let mut client = Self {
            child,
            stdin,
            rx,
            reader: Some(reader),
            timeout,
            next_id: 1,
            alive: true,
            capabilities: HashSet::new(),
        };
        let hello = client.receive(timeout)?;
        if hello["type"] != "hello" || hello["proto"].as_u64() != Some(PROTOCOL_VERSION) {
            client.disable();
            return Err(LoreError::InvalidFrame);
        }
        let caps = hello["capabilities"]
            .as_array()
            .ok_or(LoreError::InvalidFrame)?;
        if !["scrub", "snapshot"]
            .iter()
            .all(|name| caps.iter().any(|cap| cap.as_str() == Some(name)))
        {
            client.disable();
            return Err(LoreError::Unavailable);
        }
        client.capabilities = caps
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect();
        Ok(client)
    }

    pub fn scrub(&mut self, text: &str) -> Result<String, LoreError> {
        self.request_text(json!({"op":"scrub","text":text}))
    }

    pub fn snapshot(&mut self, cwd: &str, scope: &str) -> Result<String, LoreError> {
        if cwd.is_empty()
            || cwd.len() > 4096
            || cwd.contains('\0')
            || !matches!(scope, "all" | "user" | "project")
        {
            return Err(LoreError::InvalidFrame);
        }
        self.request_text(json!({"op":"snapshot","cwd":cwd,"scope":scope}))
    }

    /// Ask LORE for its actual Python 1.x transcript location. Reimplementing
    /// `project_slug` here would risk writing a second history for one project.
    pub fn transcript_identity(&mut self, cwd: &str) -> Result<(PathBuf, String), LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0') {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value("transcript_identity", json!({"cwd": cwd}))?;
        let root = value["projects_dir"]
            .as_str()
            .ok_or(LoreError::InvalidFrame)?;
        let slug = value["slug"].as_str().ok_or(LoreError::InvalidFrame)?;
        if !Path::new(root).is_absolute() || slug.is_empty() {
            return Err(LoreError::InvalidFrame);
        }
        Ok((PathBuf::from(root), slug.to_owned()))
    }

    pub fn pending(&mut self, cwd: &str, offset: u16, limit: u8) -> Result<Vec<Value>, LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0') || offset > 10000 || limit > 50
        {
            return Err(LoreError::InvalidFrame);
        }
        let value =
            self.request_value("pending", json!({"cwd":cwd,"offset":offset,"limit":limit}))?;
        let rows = value.as_array().ok_or(LoreError::InvalidFrame)?;
        if rows.len() > limit as usize
            || !rows
                .iter()
                .all(|row| row.is_object() && row["pid"].is_string())
        {
            return Err(LoreError::InvalidFrame);
        }
        Ok(rows.clone())
    }

    /// One active FTS belief. It is derived data for citation, never an instruction.
    pub fn consult(&mut self, prompt: &str) -> Result<Option<ConsultHit>, LoreError> {
        if prompt.is_empty() || prompt.len() > 8192 || prompt.contains('\0') {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value("consult", json!({"prompt":prompt}))?;
        if value.is_null() {
            return Ok(None);
        }
        if value["citation_status"] != "cite_only" {
            return Err(LoreError::InvalidFrame);
        }
        let id = value["id"]
            .as_u64()
            .filter(|id| *id > 0)
            .ok_or(LoreError::InvalidFrame)?;
        let claim = value["claim"]
            .as_str()
            .filter(|s| s.len() <= 960)
            .ok_or(LoreError::InvalidFrame)?;
        let claim_truncated = value["claim_truncated"]
            .as_bool()
            .ok_or(LoreError::InvalidFrame)?;
        let confidence = value["confidence"]
            .as_f64()
            .filter(|n| n.is_finite() && (0.0..=1.0).contains(n))
            .ok_or(LoreError::InvalidFrame)?;
        let score = value["score"]
            .as_f64()
            .filter(|n| n.is_finite())
            .ok_or(LoreError::InvalidFrame)?;
        Ok(Some(ConsultHit {
            id,
            claim: claim.to_owned(),
            claim_truncated,
            confidence,
            score,
        }))
    }

    pub fn beliefs(&mut self, offset: u16, limit: u8) -> Result<Vec<Value>, LoreError> {
        if offset > 10000 || limit > 50 {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value("beliefs", json!({"offset":offset,"limit":limit}))?;
        let rows = value
            .as_array()
            .filter(|rows| rows.len() <= limit as usize)
            .ok_or(LoreError::InvalidFrame)?;
        if !rows.iter().all(|row| {
            row["id"].as_u64().is_some_and(|id| id > 0)
                && row["subject"].is_string()
                && row["claim"].is_string()
                && row["claim_truncated"].is_boolean()
                && row["confidence"]
                    .as_f64()
                    .is_some_and(|n| n.is_finite() && (0.0..=1.0).contains(&n))
                && row["evidence_count"].as_u64().is_some()
        }) {
            return Err(LoreError::InvalidFrame);
        }
        Ok(rows.clone())
    }

    pub fn evidence(&mut self, belief_id: u64, limit: u8) -> Result<Vec<Value>, LoreError> {
        if belief_id == 0 || belief_id > i64::MAX as u64 || limit > 50 {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value("evidence", json!({"belief_id":belief_id,"limit":limit}))?;
        let rows = value
            .as_array()
            .filter(|rows| rows.len() <= limit as usize)
            .ok_or(LoreError::InvalidFrame)?;
        if !rows.iter().all(|row| {
            row["session_id"].is_string()
                && row["project"].is_string()
                && row["note"].is_string()
                && row["note_truncated"].is_boolean()
                && row["created"].is_string()
                && (row.get("source_engine").is_none() || row["source_engine"].is_string())
                && (row.get("trail_truncated").is_none() || row["trail_truncated"].is_boolean())
        }) {
            return Err(LoreError::InvalidFrame);
        }
        Ok(rows.clone())
    }

    pub fn sync_state(&mut self) -> Result<Option<SyncState>, LoreError> {
        let value = self.request_value("sync_state", json!({}))?;
        if value.is_null() {
            return Ok(None);
        }
        let age = match &value["last_pull_age_s"] {
            Value::Null => None,
            v => Some(
                v.as_f64()
                    .filter(|n| n.is_finite() && *n >= 0.0)
                    .ok_or(LoreError::InvalidFrame)?,
            ),
        };
        Ok(Some(SyncState {
            last_pull_age_s: age,
            unpushed: value["unpushed"].as_u64().ok_or(LoreError::InvalidFrame)?,
            conflicts: value["conflicts"].as_u64().ok_or(LoreError::InvalidFrame)?,
            unverified: value["unverified"]
                .as_u64()
                .ok_or(LoreError::InvalidFrame)?,
        }))
    }

    pub fn refresh_interval(&mut self) -> Result<Option<u64>, LoreError> {
        let value = self.request_value("refresh_interval", json!({}))?;
        if value.is_null() {
            Ok(None)
        } else {
            value.as_u64().map(Some).ok_or(LoreError::InvalidFrame)
        }
    }

    fn request_text(&mut self, frame: Value) -> Result<String, LoreError> {
        let value = self.request(frame)?;
        value["text"].as_str().map(str::to_owned).ok_or_else(|| {
            self.disable();
            LoreError::InvalidFrame
        })
    }

    fn request_value(&mut self, op: &str, mut frame: Value) -> Result<Value, LoreError> {
        if !self.capabilities.contains(op) {
            return Err(LoreError::Unavailable);
        }
        frame["op"] = json!(op);
        let reply = self.request(frame)?;
        reply.get("value").cloned().ok_or_else(|| {
            self.disable();
            LoreError::InvalidFrame
        })
    }

    fn request(&mut self, mut frame: Value) -> Result<Value, LoreError> {
        if !self.alive {
            return Err(LoreError::Closed);
        }
        let id = self.next_id;
        self.next_id = id.checked_add(1).ok_or(LoreError::InvalidFrame)?;
        frame["id"] = json!(id);
        let bytes = encode(&frame)?;
        let started = Instant::now();
        let mut offset = 0;
        while offset < bytes.len() {
            if started.elapsed() >= self.timeout {
                self.disable();
                return Err(LoreError::Timeout);
            }
            match self.stdin.write(&bytes[offset..]) {
                Ok(0) => {
                    self.disable();
                    return Err(LoreError::Closed);
                }
                Ok(n) => offset += n,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(2))
                }
                Err(error) => {
                    self.disable();
                    return Err(LoreError::Io(error));
                }
            }
        }
        let remaining = self.timeout.saturating_sub(started.elapsed());
        if remaining.is_zero() {
            self.disable();
            return Err(LoreError::Timeout);
        }
        let reply = self.receive(remaining)?;
        if reply["type"] != "reply" || reply["id"].as_u64() != Some(id) || !reply["ok"].is_boolean()
        {
            self.disable();
            return Err(LoreError::InvalidFrame);
        }
        if reply["ok"] == true {
            Ok(reply)
        } else {
            let code = match reply["error"].as_str() {
                Some("lore_unavailable") => return Err(LoreError::Unavailable),
                Some("invalid_request") => "invalid_request",
                Some("operation_failed") => "operation_failed",
                Some("output_too_large") => "output_too_large",
                _ => "remote_error",
            };
            Err(LoreError::Remote(code))
        }
    }

    fn receive(&mut self, timeout: Duration) -> Result<Value, LoreError> {
        match self.rx.recv_timeout(timeout) {
            Ok(ReadResult::Line(bytes)) => serde_json::from_slice::<Value>(&bytes)
                .ok()
                .filter(Value::is_object)
                .ok_or_else(|| {
                    self.disable();
                    LoreError::InvalidFrame
                }),
            Ok(ReadResult::Closed) => {
                self.disable();
                Err(LoreError::Closed)
            }
            Ok(ReadResult::TooLarge) => {
                self.disable();
                Err(LoreError::FrameTooLarge)
            }
            Ok(ReadResult::Io) => {
                self.disable();
                Err(LoreError::Closed)
            }
            Err(RecvTimeoutError::Timeout) => {
                self.disable();
                Err(LoreError::Timeout)
            }
            Err(RecvTimeoutError::Disconnected) => {
                self.disable();
                Err(LoreError::Closed)
            }
        }
    }

    fn disable(&mut self) {
        if self.alive {
            self.alive = false;
            #[cfg(unix)]
            unsafe {
                libc::kill(-(self.child.id() as i32), libc::SIGKILL);
            }
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

impl Drop for LoreClient {
    fn drop(&mut self) {
        self.disable();
        // A malicious descendant could keep stdout open after its parent is
        // killed; never make drop wait indefinitely for that pipe.
        if let Some(reader) = self.reader.take() {
            for _ in 0..20 {
                if reader.is_finished() {
                    let _ = reader.join();
                    return;
                }
                thread::sleep(Duration::from_millis(5));
            }
        }
    }
}

fn encode(value: &Value) -> Result<Vec<u8>, LoreError> {
    let mut bytes = serde_json::to_vec(value).map_err(|_| LoreError::InvalidFrame)?;
    bytes.push(b'\n');
    if bytes.len() > MAX_FRAME_BYTES {
        Err(LoreError::FrameTooLarge)
    } else {
        Ok(bytes)
    }
}

fn read_frames(mut reader: impl Read, tx: mpsc::Sender<ReadResult>) {
    let mut pending = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        match reader.read(&mut chunk) {
            Ok(0) => {
                let _ = tx.send(ReadResult::Closed);
                return;
            }
            Ok(n) => {
                for byte in &chunk[..n] {
                    if pending.len() == MAX_FRAME_BYTES {
                        let _ = tx.send(ReadResult::TooLarge);
                        return;
                    }
                    pending.push(*byte);
                    if *byte == b'\n'
                        && tx
                            .send(ReadResult::Line(std::mem::take(&mut pending)))
                            .is_err()
                    {
                        return;
                    }
                }
            }
            Err(_) => {
                let _ = tx.send(ReadResult::Io);
                return;
            }
        }
    }
}

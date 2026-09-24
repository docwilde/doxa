//! A bounded process boundary to DOXA's existing Python Claude Agent SDK engine.
//! This crate deliberately does not claim native Rust SDK or feature parity.

use serde_json::{json, Value};
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::thread;
use std::time::{Duration, Instant};

pub const PROTOCOL: &str = "doxa-claude-sidecar";
pub const VERSION: u64 = 1;
pub const MAX_FRAME: usize = 64 * 1024;
const START_TIMEOUT: Duration = Duration::from_secs(15);
const WRITE_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Debug)]
pub enum Error {
    Io(io::Error),
    Timeout,
    Closed,
    Oversize,
    Protocol,
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(e) => write!(f, "sidecar I/O: {e}"),
            Self::Timeout => write!(f, "sidecar operation timed out"),
            Self::Closed => write!(f, "sidecar closed"),
            Self::Oversize => write!(f, "sidecar frame exceeds 64 KiB"),
            Self::Protocol => write!(f, "invalid sidecar protocol frame"),
        }
    }
}
impl std::error::Error for Error {}
impl From<io::Error> for Error { fn from(e: io::Error) -> Self { Self::Io(e) } }

/// Child stderr is discarded: SDK diagnostics can contain prompts or secrets.
/// The caller owns all received event data and must apply display redaction.
pub struct Bridge {
    child: Child,
    stdin: ChildStdin,
    frames: Receiver<Result<Value, Error>>,
    next_id: u64,
    terminated: bool,
}

impl Bridge {
    /// Executes an explicit Python interpreter and script, never a shell.
    pub fn spawn(python: impl AsRef<Path>, script: impl AsRef<Path>) -> Result<Self, Error> {
        let mut child = Command::new(python.as_ref())
            .arg("-u").arg(script.as_ref())
            .process_group(0)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null())
            .spawn()?;
        let stdin = child.stdin.take().ok_or(Error::Closed)?;
        let flags = unsafe { libc::fcntl(stdin.as_raw_fd(), libc::F_GETFL) };
        if flags < 0 || unsafe { libc::fcntl(stdin.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
            let _ = unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL) };
            let _ = child.wait();
            return Err(Error::Io(io::Error::last_os_error()));
        }
        let stdout = child.stdout.take().ok_or(Error::Closed)?;
        let (tx, rx) = mpsc::sync_channel(32);
        thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            loop {
                let mut bytes = Vec::new();
                match reader.by_ref().take((MAX_FRAME + 1) as u64).read_until(b'\n', &mut bytes) {
                    Ok(0) => break,
                    Ok(_) => {
                        let frame = if bytes.len() > MAX_FRAME || !bytes.ends_with(b"\n") { Err(Error::Oversize) }
                            else { serde_json::from_slice::<Value>(&bytes)
                                .map_err(|_| Error::Protocol)
                                .and_then(|v| if v.is_object() { Ok(v) } else { Err(Error::Protocol) }) };
                        let invalid = frame.is_err();
                        if tx.send(frame).is_err() || invalid { break; }
                    }
                    Err(e) => { let _ = tx.send(Err(Error::Io(e))); break; }
                }
            }
        });
        let mut bridge = Self { child, stdin, frames: rx, next_id: 1, terminated: false };
        let hello = bridge.recv(START_TIMEOUT)?;
        if hello["type"] != "hello" || hello["protocol"] != PROTOCOL || hello["version"] != VERSION {
            return Err(Error::Protocol);
        }
        Ok(bridge)
    }

    /// Acknowledgements and engine events share a stream. Calls only return
    /// the next frame; the host must keep polling to receive turn events.
    pub fn request(&mut self, method: &str, params: Value) -> Result<u64, Error> {
        self.request_with_timeout(method, params, WRITE_TIMEOUT)
    }

    /// `timeout` can shorten, but never extend, the 15-second write bound.
    pub fn request_with_timeout(&mut self, method: &str, params: Value, timeout: Duration) -> Result<u64, Error> {
        if self.terminated { return Err(Error::Closed); }
        if method.is_empty() || !params.is_object() { return Err(Error::Protocol); }
        let id = self.next_id;
        self.next_id = id.checked_add(1).ok_or(Error::Protocol)?;
        let mut bytes = serde_json::to_vec(&json!({"type":"request","id":id,"method":method,"params":params}))
            .map_err(|_| Error::Protocol)?;
        bytes.push(b'\n');
        if bytes.len() > MAX_FRAME { return Err(Error::Oversize); }
        let deadline = Instant::now() + timeout.min(WRITE_TIMEOUT);
        let mut written = 0;
        while written < bytes.len() {
            match self.stdin.write(&bytes[written..]) {
                Ok(0) => return Err(Error::Closed),
                Ok(n) => written += n,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                        self.terminate_group();
                        return Err(Error::Timeout);
                    };
                    let millis = remaining.as_millis().max(1).min(i32::MAX as u128) as i32;
                    let mut fd = libc::pollfd { fd: self.stdin.as_raw_fd(), events: libc::POLLOUT, revents: 0 };
                    let ready = unsafe { libc::poll(&mut fd, 1, millis) };
                    if ready < 0 {
                        let error = io::Error::last_os_error();
                        if error.kind() == io::ErrorKind::Interrupted { continue; }
                        return Err(Error::Io(error));
                    }
                }
                Err(e) => return Err(Error::Io(e)),
            }
            if written < bytes.len() && Instant::now() >= deadline {
                self.terminate_group();
                return Err(Error::Timeout);
            }
        }
        Ok(id)
    }

    /// A receive timeout means no frame arrived during this poll. It does not
    /// terminate an otherwise healthy idle session; callers may poll again.
    pub fn recv(&mut self, timeout: Duration) -> Result<Value, Error> {
        self.frames.recv_timeout(timeout).map_err(|e| match e {
            mpsc::RecvTimeoutError::Timeout => Error::Timeout,
            mpsc::RecvTimeoutError::Disconnected => Error::Closed,
        })?
    }

    fn terminate_group(&mut self) {
        if self.terminated { return; }
        self.terminated = true;
        // The sidecar is its own process-group leader. Its SDK CLI children
        // inherit the group unless they explicitly detach.
        let _ = unsafe { libc::kill(-(self.child.id() as i32), libc::SIGKILL) };
        let _ = self.child.wait();
    }

}

impl Drop for Bridge {
    fn drop(&mut self) {
        self.terminate_group();
    }
}

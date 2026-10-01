//! Claude Code's bidirectional stream-json protocol. No SDK or interpreter.
use crate::Error;
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Read, Write},
    os::{fd::AsRawFd, unix::process::CommandExt},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc::{self, Receiver},
    thread,
    time::{Duration, Instant},
};
pub const MAX_CLI_FRAME: usize = 1024 * 1024;
pub struct CliOptions<'a> {
    pub executable: &'a Path,
    pub cwd: &'a Path,
    pub session_id: &'a str,
    pub resume: bool,
    pub model: Option<&'a str>,
    pub effort: Option<&'a str>,
    pub permission_mode: &'a str,
    pub config_dir: &'a Path,
    pub plugins: &'a [PathBuf],
}
pub fn canonical_session_id(id: &str) -> bool {
    id.len() == 36
        && id.bytes().enumerate().all(|(i, b)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                b == b'-'
            } else {
                b.is_ascii_hexdigit()
            }
        })
}
pub struct Cli {
    child: Child,
    stdin: ChildStdin,
    frames: Receiver<Result<Value, Error>>,
    next: u64,
    terminated: bool,
}
impl Cli {
    pub fn spawn(o: CliOptions<'_>) -> Result<Self, Error> {
        if !canonical_session_id(o.session_id)
            || !o.cwd.is_absolute()
            || !o.config_dir.is_absolute()
        {
            return Err(Error::Protocol);
        }
        let mut command = Command::new(o.executable);
        // `host` alone still denies tool calls that need approval; `stdio`
        // registers the callback that emits can_use_tool control requests.
        command.args([
            "--print",
            "--verbose",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--permission-prompts",
            "host",
            "--permission-prompt-tool",
            "stdio",
            "--strict-mcp-config",
            "--mcp-config",
            "{\"mcpServers\":{}}",
            "--setting-sources",
            "",
            "--permission-mode",
            o.permission_mode,
        ]);
        command
            .arg(if o.resume { "--resume" } else { "--session-id" })
            .arg(o.session_id);
        if let Some(model) = o.model {
            command.args(["--model", model]);
        }
        if let Some(effort) = o.effort {
            command.args(["--effort", effort]);
        }
        for plugin in o.plugins {
            command.arg("--plugin-dir").arg(plugin);
        }
        command
            .current_dir(o.cwd)
            .env("CLAUDE_CONFIG_DIR", o.config_dir)
            .env("LORE_SKIP", "1")
            .env("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
            .env("DOXA_PEER_INBOUND_TURNS", "0")
            .env_remove("CLAUDE_CODE_SIMPLE")
            .env_remove("CLAUDE_CODE_SAFE_MODE")
            .process_group(0)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let mut child = command.spawn()?;
        let stdin = child.stdin.take().ok_or(Error::Closed)?;
        let flags = unsafe { libc::fcntl(stdin.as_raw_fd(), libc::F_GETFL) };
        if flags < 0
            || unsafe { libc::fcntl(stdin.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) }
                < 0
        {
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
            let _ = child.wait();
            return Err(Error::Io(std::io::Error::last_os_error()));
        }
        let stdout = child.stdout.take().ok_or(Error::Closed)?;
        let (tx, rx) = mpsc::sync_channel(64);
        thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            loop {
                let mut bytes = Vec::new();
                let result = match reader
                    .by_ref()
                    .take((MAX_CLI_FRAME + 1) as u64)
                    .read_until(b'\n', &mut bytes)
                {
                    Ok(0) => break,
                    Ok(_) => {
                        if bytes.len() > MAX_CLI_FRAME || !bytes.ends_with(b"\n") {
                            Err(Error::Oversize)
                        } else {
                            serde_json::from_slice::<Value>(&bytes)
                                .map_err(|_| Error::Protocol)
                                .and_then(|v| {
                                    if v.is_object() {
                                        Ok(v)
                                    } else {
                                        Err(Error::Protocol)
                                    }
                                })
                        }
                    }
                    Err(e) => Err(Error::Io(e)),
                };
                let bad = result.is_err();
                if tx.send(result).is_err() || bad {
                    break;
                }
            }
        });
        Ok(Self {
            child,
            stdin,
            frames: rx,
            next: 1,
            terminated: false,
        })
    }
    pub fn send(&mut self, value: Value) -> Result<(), Error> {
        self.send_timeout(value, Duration::from_secs(10))
    }
    pub fn send_timeout(&mut self, value: Value, timeout: Duration) -> Result<(), Error> {
        if self.terminated {
            return Err(Error::Closed);
        }
        let mut bytes = serde_json::to_vec(&value).map_err(|_| Error::Protocol)?;
        bytes.push(b'\n');
        if bytes.len() > MAX_CLI_FRAME {
            return Err(Error::Oversize);
        }
        let deadline = Instant::now() + timeout.min(Duration::from_secs(15));
        let mut offset = 0;
        while offset < bytes.len() {
            match self.stdin.write(&bytes[offset..]) {
                Ok(0) => return Err(Error::Closed),
                Ok(n) => offset += n,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    let left = deadline.saturating_duration_since(Instant::now());
                    if left.is_zero() {
                        self.terminate();
                        return Err(Error::Timeout);
                    }
                    let mut fd = libc::pollfd {
                        fd: self.stdin.as_raw_fd(),
                        events: libc::POLLOUT,
                        revents: 0,
                    };
                    if unsafe {
                        libc::poll(
                            &mut fd,
                            1,
                            left.as_millis().max(1).min(i32::MAX as u128) as i32,
                        )
                    } < 0
                        && std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted
                    {
                        return Err(Error::Io(std::io::Error::last_os_error()));
                    }
                }
                Err(e) => return Err(Error::Io(e)),
            }
            if offset < bytes.len() && Instant::now() >= deadline {
                self.terminate();
                return Err(Error::Timeout);
            }
        }
        Ok(())
    }
    pub fn control(&mut self, request: Value) -> Result<String, Error> {
        let id = format!("doxa-{}", self.next);
        self.next = self.next.checked_add(1).ok_or(Error::Protocol)?;
        self.send(json!({"type":"control_request","request_id":id,"request":request}))?;
        Ok(id)
    }
    pub fn respond(&mut self, id: &str, response: Result<Value, &str>) -> Result<(), Error> {
        self.send(json!({"type":"control_response","response":match response{Ok(value)=>json!({"subtype":"success","request_id":id,"response":value}),Err(_)=>json!({"subtype":"error","request_id":id,"error":"DOXA refused unsupported or invalid provider control request"})}}))
    }
    pub fn prompt(&mut self, text: &str, session: &str) -> Result<(), Error> {
        self.send(json!({"type":"user","session_id":session,"message":{"role":"user","content":text},"parent_tool_use_id":null}))
    }
    pub fn recv(&mut self, timeout: Duration) -> Result<Value, Error> {
        self.frames.recv_timeout(timeout).map_err(|e| match e {
            mpsc::RecvTimeoutError::Timeout => Error::Timeout,
            mpsc::RecvTimeoutError::Disconnected => Error::Closed,
        })?
    }
    pub fn terminate(&mut self) {
        if !self.terminated {
            self.terminated = true;
            unsafe {
                libc::kill(-(self.child.id() as i32), libc::SIGKILL);
            }
            let _ = self.child.wait();
        }
    }
}
impl Drop for Cli {
    fn drop(&mut self) {
        self.terminate()
    }
}

//! Experimental, isolated Codex app-server protocol adapter. This module is not
//! selected by the daemon yet: a production switch also needs a UI approval
//! bridge and a recovery migration for existing `codex exec` thread records.
//!
//! The shapes below come from `codex app-server generate-ts --experimental`
//! (Codex 0.156.1). Stdio is newline-delimited JSON-RPC without LSP headers.
use std::io;
use std::os::unix::process::CommandExt;
use std::collections::VecDeque;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};
use tokio::time::timeout;
use tokio_util::sync::CancellationToken;

use crate::codex_driver::{valid_thread_id, SandboxMode};
use crate::EngineEvent;

const MAX_FRAME_BYTES: usize = 8 * 1024 * 1024;
const MAX_REASONING_BYTES: usize = 256 * 1024;
const RPC_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Clone, Debug)]
pub struct AppServerOptions {
    pub executable: PathBuf,
    pub cwd: PathBuf,
    pub model: Option<String>,
    pub sandbox: SandboxMode,
    pub resume_thread: Option<String>,
    pub turn_timeout: Duration,
}

#[derive(Debug)]
pub enum AppServerError {
    Io(io::Error),
    Protocol(&'static str),
    Server(String),
    Cancelled,
    TimedOut,
}

impl From<io::Error> for AppServerError {
    fn from(value: io::Error) -> Self { Self::Io(value) }
}

pub struct AppServerDriver {
    options: AppServerOptions,
    scrub: Box<dyn Fn(&str) -> String + Send + Sync>,
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next_id: u64,
    thread_id: Option<String>,
    turn_id: Option<String>,
    reasoning_bytes: usize,
    usage: Option<Value>,
    pending_notifications: VecDeque<Value>,
}

impl AppServerDriver {
    /// Spawn is intentionally separate from DOXA's current `CodexHost`.
    /// Each driver owns one child and one provider thread, including on resume.
    pub async fn spawn(
        options: AppServerOptions,
        scrub: impl Fn(&str) -> String + Send + Sync + 'static,
    ) -> Result<Self, AppServerError> {
        if options.resume_thread.as_deref().is_some_and(|id| !valid_thread_id(id)) {
            return Err(AppServerError::Protocol("invalid resume thread ID"));
        }
        let mut command = Command::new(&options.executable);
        command.arg("app-server").arg("--stdio")
            .current_dir(&options.cwd).stdin(Stdio::piped())
            .stdout(Stdio::piped()).stderr(Stdio::null()).kill_on_drop(true);
        unsafe {
            command.as_std_mut().pre_exec(|| {
                if libc::setsid() == -1 { Err(io::Error::last_os_error()) } else { Ok(()) }
            });
        }
        let mut child = command.spawn()?;
        let stdin = child.stdin.take().ok_or(AppServerError::Protocol("missing stdin"))?;
        let stdout = BufReader::new(child.stdout.take().ok_or(AppServerError::Protocol("missing stdout"))?);
        let mut driver = Self {
            options, scrub: Box::new(scrub), child, stdin, stdout, next_id: 0,
            thread_id: None, turn_id: None, reasoning_bytes: 0, usage: None,
            pending_notifications: VecDeque::new(),
        };
        driver.request("initialize", json!({"clientInfo":{"name":"doxa","title":null,"version":env!("CARGO_PKG_VERSION")},"capabilities":null})).await?;
        driver.send(json!({"method":"initialized"})).await?;
        let result = if let Some(id) = driver.options.resume_thread.clone() {
            driver.request("thread/resume", json!({"threadId":id,"cwd":driver.options.cwd,"model":driver.options.model,"approvalPolicy":"never","excludeTurns":true})).await?
        } else {
            driver.request("thread/start", json!({"cwd":driver.options.cwd,"model":driver.options.model,"approvalPolicy":"never","sandbox":sandbox_name(driver.options.sandbox)})).await?
        };
        let id = result.pointer("/thread/id").and_then(Value::as_str)
            .filter(|id| valid_thread_id(id))
            .ok_or(AppServerError::Protocol("thread response lacks a valid ID"))?;
        if driver.options.resume_thread.as_deref().is_some_and(|expected| expected != id) {
            return Err(AppServerError::Protocol("resume returned a different thread ID"));
        }
        driver.thread_id = Some(id.to_owned());
        Ok(driver)
    }

    pub fn thread_id(&self) -> &str { self.thread_id.as_deref().expect("thread start succeeded") }

    /// The caller must persist this ID *before* submitting a turn. The
    /// daemon integration will retain the existing incomplete-turn guard.
    pub async fn run_turn(
        &mut self, prompt: &str, cancel: &CancellationToken,
        mut emit: impl FnMut(EngineEvent),
    ) -> Result<(), AppServerError> {
        self.reasoning_bytes = 0;
        self.usage = None;
        let deadline = tokio::time::Instant::now() + self.options.turn_timeout;
        let thread_id = self.thread_id().to_owned();
        let request_id = self.send_request("turn/start", json!({"threadId":thread_id,"input":[{"type":"text","text":prompt,"text_elements":[]}]})).await?;
        let response = self.wait_response(request_id, Some(cancel), deadline).await?;
        let turn_id = response.pointer("/turn/id").and_then(Value::as_str)
            .filter(|id| valid_thread_id(id))
            .ok_or(AppServerError::Protocol("turn response lacks a valid ID"))?.to_owned();
        self.turn_id = Some(turn_id.clone());
        loop {
            let frame = if let Some(frame) = self.pending_notifications.pop_front() { frame } else {
                tokio::select! {
                    value = self.read_frame() => value?,
                    _ = cancel.cancelled() => {
                        let _ = self.send_request("turn/interrupt", json!({"threadId":thread_id,"turnId":turn_id})).await;
                        return Err(AppServerError::Cancelled);
                    }
                    _ = tokio::time::sleep_until(deadline) => return Err(AppServerError::TimedOut),
                }
            };
            if let Some(method) = frame.get("method").and_then(Value::as_str) {
                if frame.get("id").is_some() {
                    self.deny_server_request(&frame).await?;
                    continue;
                }
                let params = &frame["params"];
                if params["threadId"].as_str().is_some_and(|id| id != thread_id) { continue; }
                if params["turnId"].as_str().is_some_and(|id| id != turn_id) { continue; }
                if method == "turn/completed" && params["turn"]["id"].as_str() != Some(&turn_id) {
                    continue;
                }
                match method {
                    "item/agentMessage/delta" => self.emit_text(params, "text_delta", &mut emit),
                    "item/reasoning/textDelta" | "item/reasoning/summaryTextDelta" => {
                        if let Some(raw) = params["delta"].as_str() {
                            let remaining = MAX_REASONING_BYTES.saturating_sub(self.reasoning_bytes);
                            let mut end = raw.len().min(remaining);
                            while !raw.is_char_boundary(end) { end -= 1; }
                            if end > 0 {
                                self.reasoning_bytes += end;
                                let text = (self.scrub)(&raw[..end]);
                                emit(EngineEvent::new("reasoning_delta", json!({"text":text,"approx_tokens":self.reasoning_bytes / 4,"count_is_estimate":true})));
                            }
                        }
                    }
                    "thread/tokenUsage/updated" => {
                        self.usage = Some(params["tokenUsage"].clone());
                        let last = &params["tokenUsage"]["last"];
                        let context_window = params["tokenUsage"]["modelContextWindow"].as_u64();
                        emit(EngineEvent::new("usage", json!({
                            "input_tokens":last["inputTokens"],"output_tokens":last["outputTokens"],
                            "cache_read_input_tokens":last["cachedInputTokens"],
                            "reasoning_output_tokens":last["reasoningOutputTokens"],
                            "context_window":context_window,"context_used":last["totalTokens"],
                            "reasoning_count_is_estimate":false
                        })));
                    }
                    "turn/completed" => {
                        let status = params["turn"]["status"].as_str().unwrap_or("failed");
                        let failed = status != "completed";
                        let error = params["turn"]["error"]["message"].as_str().map(|s| (self.scrub)(s));
                        emit(EngineEvent::new("turn_done", json!({"is_error":failed,"error":error,"reasoning_output_tokens":self.usage.as_ref().and_then(|u| u.pointer("/last/reasoningOutputTokens"))})));
                        self.turn_id = None;
                        return if failed { Err(AppServerError::Server(error.unwrap_or_else(|| format!("turn {status}")))) } else { Ok(()) };
                    }
                    "error" => return Err(AppServerError::Server((self.scrub)(params["error"].as_str().unwrap_or("Codex app-server error")))),
                    _ => {},
                }
            }
        }
    }

    fn emit_text(&self, params: &Value, kind: &str, emit: &mut impl FnMut(EngineEvent)) {
        if let Some(delta) = params["delta"].as_str() {
            emit(EngineEvent::new(kind, json!({"text":(self.scrub)(delta)})));
        }
    }

    async fn request(&mut self, method: &str, params: Value) -> Result<Value, AppServerError> {
        let id = self.send_request(method, params).await?;
        timeout(RPC_TIMEOUT, self.wait_response(id, None, tokio::time::Instant::now() + RPC_TIMEOUT))
            .await.map_err(|_| AppServerError::TimedOut)?
    }

    async fn send_request(&mut self, method: &str, params: Value) -> Result<u64, AppServerError> {
        self.next_id += 1;
        self.send(json!({"id":self.next_id,"method":method,"params":params})).await?;
        Ok(self.next_id)
    }

    async fn send(&mut self, frame: Value) -> Result<(), AppServerError> {
        let mut encoded = serde_json::to_vec(&frame).map_err(io::Error::other)?;
        if encoded.len() > MAX_FRAME_BYTES { return Err(AppServerError::Protocol("outgoing frame too large")); }
        encoded.push(b'\n');
        self.stdin.write_all(&encoded).await?;
        self.stdin.flush().await?;
        Ok(())
    }

    async fn read_frame(&mut self) -> Result<Value, AppServerError> {
        let mut line = Vec::new();
        loop {
            let available = self.stdout.fill_buf().await?;
            if available.is_empty() { return Err(AppServerError::Protocol("app-server closed stdout")); }
            let take = available.iter().position(|byte| *byte == b'\n').map_or(available.len(), |at| at + 1);
            if line.len().saturating_add(take) > MAX_FRAME_BYTES {
                return Err(AppServerError::Protocol("app-server frame too large"));
            }
            let done = available[take - 1] == b'\n';
            line.extend_from_slice(&available[..take]);
            self.stdout.consume(take);
            if done { break; }
        }
        serde_json::from_slice(&line).map_err(|_| AppServerError::Protocol("invalid app-server JSON frame"))
    }

    async fn wait_response(&mut self, id: u64, cancel: Option<&CancellationToken>, deadline: tokio::time::Instant) -> Result<Value, AppServerError> {
        loop {
            let frame = tokio::select! {
                value = self.read_frame() => value?,
                _ = async { if let Some(token) = cancel { token.cancelled().await } else { std::future::pending().await } } => return Err(AppServerError::Cancelled),
                _ = tokio::time::sleep_until(deadline) => return Err(AppServerError::TimedOut),
            };
            if frame["id"].as_u64() == Some(id) {
                if !frame["error"].is_null() { return Err(AppServerError::Server((self.scrub)(&frame["error"].to_string()))); }
                return Ok(frame["result"].clone());
            }
            if frame.get("method").is_some() && frame.get("id").is_some() {
                self.deny_server_request(&frame).await?;
            } else if frame.get("method").is_some() {
                if self.pending_notifications.len() >= 256 {
                    return Err(AppServerError::Protocol("too many early app-server notifications"));
                }
                self.pending_notifications.push_back(frame);
            }
        }
    }

    /// There is no DOXA approval bridge in this slice. Deny every server
    /// request explicitly; never silently grant shell, patch, or new tools.
    async fn deny_server_request(&mut self, frame: &Value) -> Result<(), AppServerError> {
        let Some(id) = frame.get("id") else { return Ok(()); };
        let result = match frame["method"].as_str().unwrap_or("") {
            "item/commandExecution/requestApproval" => json!({"decision":"decline"}),
            "item/fileChange/requestApproval" => json!({"decision":"decline"}),
            _ => { self.send(json!({"id":id,"error":{"code":-32601,"message":"DOXA cannot handle this server request"}})).await?; return Ok(()); }
        };
        self.send(json!({"id":id,"result":result})).await
    }
}

impl Drop for AppServerDriver {
    fn drop(&mut self) {
        if let Some(pid) = self.child.id() {
            unsafe { libc::kill(-(pid as i32), libc::SIGKILL); }
        }
        let _ = self.child.start_kill();
    }
}

fn sandbox_name(mode: SandboxMode) -> &'static str {
    match mode {
        SandboxMode::ReadOnly => "read-only",
        SandboxMode::WorkspaceWrite => "workspace-write",
        SandboxMode::DangerFullAccess => "danger-full-access",
    }
}

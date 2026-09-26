//! Bounded Codex app-server protocol adapter. Interactive server requests are
//! explicitly refused until DOXA has a matching approval UI bridge.
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
use crate::codex::CodexJsonlNormalizer;
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
    reasoning_chars: usize,
    reasoning_buffer: String,
    reasoning_truncated: bool,
    assistant_buffers: Vec<(String, String)>,
    assistant_bytes: usize,
    usage: Option<Value>,
    pending_notifications: VecDeque<(Value, usize)>,
    pending_bytes: usize,
    tool_normalizer: CodexJsonlNormalizer,
}

impl AppServerDriver {
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
        let scrub = std::sync::Arc::new(scrub);
        let tool_scrub = scrub.clone();
        let mut driver = Self {
            options, scrub: Box::new(move |text| scrub(text)), child, stdin, stdout, next_id: 0,
            thread_id: None, turn_id: None, reasoning_bytes: 0, reasoning_chars: 0,
            reasoning_buffer: String::new(), reasoning_truncated: false,
            assistant_buffers: Vec::new(), assistant_bytes: 0, usage: None,
            pending_notifications: VecDeque::new(),
            pending_bytes: 0,
            tool_normalizer: CodexJsonlNormalizer::new(move |text| tool_scrub(text)),
        };
        driver.request("initialize", json!({"clientInfo":{"name":"doxa","title":null,"version":env!("CARGO_PKG_VERSION")},"capabilities":null})).await?;
        driver.send(json!({"method":"initialized"})).await?;
        let result = if let Some(id) = driver.options.resume_thread.clone() {
            driver.request("thread/resume", json!({"threadId":id,"cwd":driver.options.cwd,"model":driver.options.model,"approvalPolicy":"never","sandbox":sandbox_name(driver.options.sandbox),"excludeTurns":true})).await?
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

    pub async fn shutdown(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            if let Some(pid) = self.child.id() {
                unsafe { libc::kill(-(pid as i32), libc::SIGKILL); }
            }
        }
        let _ = self.child.start_kill();
        let _ = timeout(Duration::from_secs(5), self.child.wait()).await;
    }

    /// The caller must persist this ID *before* submitting a turn. The
    /// daemon integration will retain the existing incomplete-turn guard.
    pub async fn run_turn(
        &mut self, prompt: &str, cancel: &CancellationToken,
        mut emit: impl FnMut(EngineEvent),
    ) -> Result<(), AppServerError> {
        self.reasoning_bytes = 0;
        self.reasoning_chars = 0;
        self.reasoning_buffer.clear();
        self.reasoning_truncated = false;
        self.assistant_buffers.clear();
        self.assistant_bytes = 0;
        self.usage = None;
        self.tool_normalizer.begin_turn();
        let deadline = tokio::time::Instant::now() + self.options.turn_timeout;
        let thread_id = self.thread_id().to_owned();
        let request_id = self.send_request("turn/start", json!({"threadId":thread_id,"input":[{"type":"text","text":prompt,"text_elements":[]}]})).await?;
        let response = self.wait_response(request_id, Some(cancel), deadline).await?;
        let turn_id = response.pointer("/turn/id").and_then(Value::as_str)
            .filter(|id| valid_thread_id(id))
            .ok_or(AppServerError::Protocol("turn response lacks a valid ID"))?.to_owned();
        self.turn_id = Some(turn_id.clone());
        loop {
            let frame = if let Some((frame, bytes)) = self.pending_notifications.pop_front() {
                self.pending_bytes = self.pending_bytes.saturating_sub(bytes);
                frame
            } else {
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
                if method != "error" && params["threadId"].as_str() != Some(&thread_id) { continue; }
                if method != "error" && method != "turn/completed"
                    && params["turnId"].as_str() != Some(&turn_id) { continue; }
                if method == "turn/completed" && params["turn"]["id"].as_str() != Some(&turn_id) {
                    continue;
                }
                match method {
                    "item/agentMessage/delta" => {
                        if let (Some(id), Some(delta)) = (params["itemId"].as_str(), params["delta"].as_str()) {
                            if self.assistant_bytes.saturating_add(delta.len()) > MAX_FRAME_BYTES {
                                return Err(AppServerError::Protocol("assistant turn text exceeded display limit"));
                            }
                            self.assistant_bytes += delta.len();
                            if let Some((_, text)) = self.assistant_buffers.iter_mut().find(|(key, _)| key == id) {
                                text.push_str(delta);
                            } else {
                                if self.assistant_buffers.len() >= 128 { return Err(AppServerError::Protocol("too many assistant items")); }
                                self.assistant_buffers.push((id.to_owned(), delta.to_owned()));
                            }
                        }
                    }
                    "item/reasoning/textDelta" | "item/reasoning/summaryTextDelta" => {
                        if let Some(raw) = params["delta"].as_str() {
                            self.reasoning_chars = self.reasoning_chars.saturating_add(raw.chars().count());
                            let remaining = MAX_REASONING_BYTES.saturating_sub(self.reasoning_bytes);
                            let mut end = raw.len().min(remaining);
                            while !raw.is_char_boundary(end) { end -= 1; }
                            self.reasoning_truncated |= end < raw.len();
                            if end > 0 {
                                self.reasoning_bytes += end;
                                self.reasoning_buffer.push_str(&raw[..end]);
                            }
                            emit(EngineEvent::new("reasoning_progress", json!({"approx_tokens":self.reasoning_chars / 4,"count_is_estimate":true})));
                        }
                    }
                    "thread/tokenUsage/updated" => {
                        self.usage = Some(params["tokenUsage"].clone());
                        let last = &params["tokenUsage"]["last"];
                        let context_window = params["tokenUsage"]["modelContextWindow"].as_u64();
                        emit(EngineEvent::new("usage", json!({
                            "input_tokens":last["inputTokens"].as_u64(),"output_tokens":last["outputTokens"].as_u64(),
                            "cache_read_input_tokens":last["cachedInputTokens"].as_u64(),
                            "reasoning_output_tokens":last["reasoningOutputTokens"].as_u64(),
                            "context_window":context_window,"context_used":last["totalTokens"].as_u64(),
                            "reasoning_count_is_estimate":false
                        })));
                    }
                    "item/started" | "item/completed" => {
                        if method == "item/completed" && params["item"]["type"] == "agentMessage" {
                            if let Some(id) = params["item"]["id"].as_str() {
                                let buffered = self.assistant_buffers.iter().position(|(key, _)| key == id)
                                    .map(|index| self.assistant_buffers.remove(index).1);
                                let text = params["item"]["text"].as_str().map(str::to_owned).or(buffered).unwrap_or_default();
                                if text.len() > MAX_FRAME_BYTES { return Err(AppServerError::Protocol("assistant item exceeded display limit")); }
                                emit(EngineEvent::new("text_delta", json!({"text":(self.scrub)(&text)})));
                            }
                        }
                        if let Some(item) = normalize_tool_item(&params["item"]) {
                            let kind = if method == "item/started" { "item.started" } else { "item.completed" };
                            let line = format!("{}\n", json!({"type":kind,"item":item}));
                            for event in self.tool_normalizer.push_bytes(line.as_bytes())
                                .map_err(|_| AppServerError::Protocol("tool event too large"))? {
                                emit(event);
                            }
                        }
                    }
                    "turn/completed" => {
                        let status = params["turn"]["status"].as_str().unwrap_or("failed");
                        let failed = status != "completed";
                        let error = params["turn"]["error"]["message"].as_str().map(|s| (self.scrub)(s));
                        if !self.reasoning_buffer.is_empty() {
                            let text = if self.reasoning_truncated { "[Reasoning content withheld: display limit reached]".to_owned() }
                                else { (self.scrub)(&self.reasoning_buffer) };
                            emit(EngineEvent::new("reasoning_delta", json!({"text":text,"approx_tokens":self.reasoning_chars / 4,"count_is_estimate":true,"final":true})));
                        }
                        for (_, text) in self.assistant_buffers.drain(..) {
                            emit(EngineEvent::new("text_delta", json!({"text":(self.scrub)(&text)})));
                        }
                        let total = self.usage.as_ref().map(|u| &u["total"]);
                        let last = self.usage.as_ref().map(|u| &u["last"]);
                        let window = self.usage.as_ref().and_then(|u| u["modelContextWindow"].as_u64()).and_then(|window| window.checked_sub(12_000)).filter(|window| *window > 0);
                        let used = last.and_then(|u| u["totalTokens"].as_u64()).map(|used| used.saturating_sub(12_000));
                        let pct = used.zip(window).and_then(|(used, window)|
                            (window > 0 && used <= window).then_some(100.0 * used as f64 / window as f64));
                        emit(EngineEvent::new("turn_done", json!({
                            "is_error":failed,"error":error,"usage_scope":"session",
                            "usage_source":"codex_app_server_token_usage_updated",
                            "input_tokens":total.and_then(|u| u["inputTokens"].as_u64()),
                            "output_tokens":total.and_then(|u| u["outputTokens"].as_u64()),
                            "cache_read_input_tokens":total.and_then(|u| u["cachedInputTokens"].as_u64()),
                            "reasoning_output_tokens":last.and_then(|u| u["reasoningOutputTokens"].as_u64()),
                            "ctx_tokens":used,"ctx_max_tokens":window,"ctx_percentage":pct,
                            "cost_usd":null,"session_cost_usd":null,
                        })));
                        self.turn_id = None;
                        return if failed { Err(AppServerError::Server(error.unwrap_or_else(|| format!("turn {status}")))) } else { Ok(()) };
                    }
                    "error" => return Err(AppServerError::Server((self.scrub)(params["error"].as_str().unwrap_or("Codex app-server error")))),
                    _ => {},
                }
            }
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
                let bytes = serde_json::to_vec(&frame).map_err(io::Error::other)?.len();
                if self.pending_notifications.len() >= 256 || self.pending_bytes.saturating_add(bytes) > MAX_FRAME_BYTES {
                    return Err(AppServerError::Protocol("too many early app-server notifications"));
                }
                self.pending_bytes += bytes;
                self.pending_notifications.push_back((frame, bytes));
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
            _ => {
                self.send(json!({"id":id,"error":{"code":-32601,"message":"DOXA cannot handle this server request"}})).await?;
                return Err(AppServerError::Server("Codex requested an interactive tool or approval that DOXA cannot handle; the request was refused".to_owned()));
            }
        };
        self.send(json!({"id":id,"result":result})).await?;
        Err(AppServerError::Server("Codex requested interactive approval; DOXA refused it because app-server approval dialogs are not supported yet".to_owned()))
    }
}

impl Drop for AppServerDriver {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            if let Some(pid) = self.child.id() {
            unsafe { libc::kill(-(pid as i32), libc::SIGKILL); }
            }
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

/// Adapt only tool items to the existing scrubbed and bounded Codex tool
/// normalizer. Message and reasoning items arrive as live deltas and must not
/// be replayed from their final snapshots.
fn normalize_tool_item(item: &Value) -> Option<Value> {
    let id = item["id"].as_str()?;
    if !valid_thread_id(id) { return None; }
    let value = match item["type"].as_str()? {
        "commandExecution" => json!({
            "type":"command_execution","id":id,"command":item["command"],
            "status":item["status"],"aggregated_output":item["aggregatedOutput"],
            "exit_code":item["exitCode"]
        }),
        "fileChange" => json!({
            "type":"file_change","id":id,"status":item["status"],
            "changes":item["changes"]
        }),
        "mcpToolCall" => json!({
            "type":"mcp_tool_call","id":id,"status":item["status"],
            "server":item["server"],"tool":item["tool"],
            "arguments":item["arguments"],"result":item["result"],
            "error":item["error"]
        }),
        "webSearch" => json!({
            "type":"web_search","id":id,"query":item["query"],
            "status":item["status"]
        }),
        _ => return None,
    };
    Some(value)
}

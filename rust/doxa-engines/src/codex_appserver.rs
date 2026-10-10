//! Bounded Codex app-server protocol adapter with explicit request routing.
//! Interactive requests require a host bridge; unknown requests are refused.
//!
//! The shapes below come from `codex app-server generate-ts --experimental`
//! (Codex 0.156.1). Stdio is newline-delimited JSON-RPC without LSP headers.
use std::io;
use std::os::unix::{process::CommandExt, net::UnixStream, io::AsRawFd};
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

#[path = "codex_appserver_compact.rs"]
mod reviewed_compact;

const MAX_FRAME_BYTES: usize = 8 * 1024 * 1024;
const MAX_REASONING_BYTES: usize = 256 * 1024;
const RPC_TIMEOUT: Duration = Duration::from_secs(15);

/// Read the provider's product/version prefix, never a client or platform suffix.
fn provider_version(agent: &str) -> Option<&str> {
    let prefix = agent.split('(').next()?.trim();
    let (product, rest) = prefix.split_once('/')?;
    if !matches!(product, "codex_cli_rs" | "Codex Desktop" | "doxa_codex_rs") { return None; }
    let version = rest.split_whitespace().next()?;
    let parts = version.split('.').collect::<Vec<_>>();
    (parts.len() == 3 && parts.iter().all(|part| !part.is_empty() && part.bytes().all(|c| c.is_ascii_digit())))
        .then_some(version)
}

#[derive(Clone, Debug)]
pub struct AppServerOptions {
    pub executable: PathBuf,
    pub cwd: PathBuf,
    pub model: Option<String>,
    pub sandbox: SandboxMode,
    pub permission: CodexPermission,
    pub resume_thread: Option<String>,
    pub turn_timeout: Duration,
}

/// DOXA labels for Codex turn overrides. Auto skips provider approval prompts
/// inside a sandbox; it is distinct from Claude's classifier-backed auto mode.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CodexPermission {
    OnRequest,
    Auto,
    FullAccess,
}

impl CodexPermission {
    pub fn from_mode(mode: &str) -> Option<Self> {
        match mode {
            "on-request" => Some(Self::OnRequest),
            "auto" => Some(Self::Auto),
            "full-access" => Some(Self::FullAccess),
            _ => None,
        }
    }

    pub fn mode(self) -> &'static str {
        match self {
            Self::OnRequest => "on-request",
            Self::Auto => "auto",
            Self::FullAccess => "full-access",
        }
    }

    fn approval_policy(self) -> &'static str {
        if self == Self::OnRequest { "on-request" } else { "never" }
    }

    fn sandbox(self, configured: SandboxMode) -> SandboxMode {
        match self {
            Self::FullAccess => SandboxMode::DangerFullAccess,
            Self::Auto if configured == SandboxMode::DangerFullAccess => SandboxMode::WorkspaceWrite,
            _ => configured,
        }
    }
}

#[derive(Debug)]
pub enum AppServerError {
    Io(io::Error),
    Protocol(&'static str),
    Server(String),
    Cancelled,
    /// Native pre-request review refused submission, or a verified blocking
    /// hook stopped a submitted manual compaction before context replacement.
    CompactionBlocked,
    /// Cancellation before sending any compaction request. The bound context
    /// remains safe to resume even if the source-read transport was stopped.
    CompactionCancelled,
    TimedOut,
}

impl From<io::Error> for AppServerError {
    fn from(value: io::Error) -> Self { Self::Io(value) }
}

/// Bounded control lane independent of the driver mutex held by a running turn.
pub struct LiveAutoRequest {
    pub expires: std::time::Instant,
    pub reply: std::sync::mpsc::SyncSender<Result<String, &'static str>>,
}

async fn next_live_auto(control: &mut Option<&mut tokio::sync::mpsc::Receiver<LiveAutoRequest>>) -> Option<LiveAutoRequest> {
    match control {
        Some(receiver) => receiver.recv().await,
        None => std::future::pending().await,
    }
}

fn provider_approval(method: &Value) -> bool {
    matches!(method.as_str(), Some("item/commandExecution/requestApproval" | "item/fileChange/requestApproval" | "item/permissions/requestApproval"))
}

pub struct AppServerDriver {
    options: AppServerOptions,
    effort: Option<String>,
    interactive: bool,
    live_auto_supported: bool,
    peer_tools: bool,
    agent_tools: Vec<Value>,
    dynamic_tool_names: Vec<(String, String)>,
    compact_gate: Option<crate::codex_compact::CompactGate>,
    review_items: Vec<Value>,
    scrub: Box<dyn Fn(&str) -> String + Send + Sync>,
    child: Child,
    // Keep the unreaped leader PID reserved until killing this original group.
    // Reaping first would allow PID/PGID reuse and miss surviving descendants.
    process_group: Option<u32>,
    owner_control: Option<UnixStream>,
    supervised: bool,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    frame_buffer: Vec<u8>,
    next_id: u64,
    thread_id: Option<String>,
    turn_id: Option<String>,
    reasoning_bytes: usize,
    reasoning_chars: usize,
    reasoning_buffer: String,
    reasoning_truncated: bool,
    assistant_buffers: Vec<(String, String)>,
    assistant_bytes: usize,
    assistant_message_emitted: bool,
    usage: Option<Value>,
    effective_model: Option<String>,
    pending_notifications: VecDeque<(Value, usize)>,
    pending_bytes: usize,
    tool_normalizer: CodexJsonlNormalizer,
}

fn codex_billing(reply: &Value) -> Option<Value> {
    let bucket = reply.pointer("/rateLimitsByLimitId/codex")
        .or_else(|| reply.get("rateLimits"))?;
    if bucket["limitId"] != "codex" { return None; }
    let mut windows = Vec::new();
    for name in ["primary", "secondary"] {
        let row = &bucket[name];
        let Some(used) = row["usedPercent"].as_f64()
            .filter(|used| used.is_finite() && (0.0..=100.0).contains(used)) else { continue };
        let Some(minutes) = row["windowDurationMins"].as_u64()
            .filter(|minutes| *minutes > 0 && *minutes <= 10_080) else { continue };
        let window = match minutes {
            10_080 => "week".to_owned(),
            n if n % 60 == 0 => format!("{}h", n / 60),
            n => format!("{n}m"),
        };
        windows.push(format!("{window}:{used}%"));
    }
    if windows.is_empty() { return None; }
    let plan = bucket["planType"].as_str()
        .or_else(|| reply["planType"].as_str())
        .filter(|plan| !plan.is_empty() && plan.len() <= 32
            && plan.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-'));
    Some(json!({"mode":"subscription","type":plan,"quota":windows.join(" · ")}))
}

impl AppServerDriver {
    /// Each driver owns one child and one provider thread, including on resume.
    pub async fn spawn(
        options: AppServerOptions,
        scrub: impl Fn(&str) -> String + Send + Sync + 'static,
    ) -> Result<Self, AppServerError> {
        let mut driver = Self::initialize(options, scrub).await?;
        driver.start_thread().await?;
        Ok(driver)
    }

    /// Interactive mode uses provider approvals instead of silently running
    /// commands that need escalation. Catalog discovery never creates a thread.
    pub async fn spawn_interactive(
        options: AppServerOptions,
        scrub: impl Fn(&str) -> String + Send + Sync + 'static,
    ) -> Result<Self, AppServerError> {
        Self::spawn_interactive_with_tools(options, scrub, false).await
    }

    pub async fn spawn_interactive_with_tools(
        options: AppServerOptions,
        scrub: impl Fn(&str) -> String + Send + Sync + 'static,
        peer_tools: bool,
    ) -> Result<Self, AppServerError> {
        let mut driver = Self::initialize(options, scrub).await?;
        driver.interactive = true;
        driver.peer_tools = peer_tools;
        driver.start_thread().await?;
        Ok(driver)
    }

    pub async fn spawn_protected(
        options: AppServerOptions,
        scrub: impl Fn(&str) -> String + Send + Sync + 'static,
        peer_tools: bool,
        gate: crate::codex_compact::CompactGate,
    ) -> Result<Self, AppServerError> {
        Self::spawn_protected_with_agent_tools(options, scrub, peer_tools, gate, Vec::new()).await
    }
    pub async fn spawn_protected_with_agent_tools(
        options: AppServerOptions,
        scrub: impl Fn(&str) -> String + Send + Sync + 'static,
        peer_tools: bool,
        gate: crate::codex_compact::CompactGate,
        definitions: Vec<Value>,
    ) -> Result<Self, AppServerError> {
        if definitions.len() > 6 || definitions.iter().enumerate().any(|(index,row)| definitions[..index].iter().any(|prior| prior["name"] == row["name"])) || definitions.iter().any(|row| row["name"].as_str().is_none_or(|name|
            !matches!(name, "mcp__doxa__lore_belief_search" | "mcp__doxa__lore_belief_show" | "mcp__doxa__lore_belief_neighbours" |
                "mcp__doxa__lore_memory_list" | "mcp__doxa__lore_session_search" | "mcp__doxa__lore_remember")) || row["inputSchema"]["type"] != "object") {
            return Err(AppServerError::Protocol("Invalid canonical LORE tool catalog"));
        }
        let mut driver = Self::initialize_with_gate(options, scrub, Some(gate)).await?;
        driver.agent_tools = definitions;
        driver.interactive = true;
        driver.peer_tools = peer_tools;
        driver.start_thread().await?;
        Ok(driver)
    }

    async fn initialize(options: AppServerOptions, scrub: impl Fn(&str) -> String + Send + Sync + 'static) -> Result<Self, AppServerError> {
        Self::initialize_with_gate(options, scrub, None).await
    }

    async fn initialize_with_gate(options: AppServerOptions, scrub: impl Fn(&str) -> String + Send + Sync + 'static,
        compact_gate: Option<crate::codex_compact::CompactGate>) -> Result<Self, AppServerError> {
        if options.resume_thread.as_deref().is_some_and(|id| !valid_thread_id(id)) {
            return Err(AppServerError::Protocol("invalid resume thread ID"));
        }
        let isolated = doxa_isolation::active()?.is_some();
        let supervised = compact_gate.is_some() && !isolated;
        let (mut owner_control, owner_peer) = if supervised {
            let (parent, child) = UnixStream::pair()?;
            (Some(parent), Some(child))
        } else { (None, None) };
        let owner_fd = owner_peer.as_ref().map(AsRawFd::as_raw_fd);
        let mut provider_command = std::process::Command::new(&options.executable);
        provider_command.arg("app-server").arg("--stdio").current_dir(&options.cwd);
        if let Some(gate) = &compact_gate {
            for value in gate.cli_overrides() { provider_command.arg("-c").arg(value); }
        }
        let mut command = Command::from(doxa_isolation::isolate_command(provider_command, "codex")?);
        command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).kill_on_drop(!supervised);
        if let Some(fd) = owner_fd { command.env(crate::provider_owner::CONTROL_ENV, fd.to_string()); }
        unsafe {
            command.as_std_mut().pre_exec(move || {
                if let Some(fd) = owner_fd {
                    if libc::fcntl(fd, libc::F_SETFD, 0) < 0 { return Err(io::Error::last_os_error()); }
                }
                if libc::setsid() == -1 { Err(io::Error::last_os_error()) } else { Ok(()) }
            });
        }
        let mut child = command.spawn()?;
        drop(owner_peer);
        let process_group = child.id();
        let stdin = child.stdin.take().ok_or(AppServerError::Protocol("missing stdin"))?;
        let stdout = BufReader::new(child.stdout.take().ok_or(AppServerError::Protocol("missing stdout"))?);
        let scrub = std::sync::Arc::new(scrub);
        let tool_scrub = scrub.clone();
        let mut driver = Self {
            options, effort: None, interactive: false, live_auto_supported: false, peer_tools: false, agent_tools: Vec::new(), dynamic_tool_names: Vec::new(), compact_gate, review_items: Vec::new(), scrub: Box::new(move |text| scrub(text)), child, process_group, owner_control: owner_control.take(), supervised: false, stdin, stdout, frame_buffer: Vec::new(), next_id: 0,
            thread_id: None, turn_id: None, reasoning_bytes: 0, reasoning_chars: 0,
            reasoning_buffer: String::new(), reasoning_truncated: false,
            assistant_buffers: Vec::new(), assistant_bytes: 0, assistant_message_emitted: false, usage: None, effective_model: None,
            pending_notifications: VecDeque::new(),
            pending_bytes: 0,
            tool_normalizer: CodexJsonlNormalizer::new(move |text| tool_scrub(text)),
        };
        if let Some(control) = driver.owner_control.as_mut() {
            crate::provider_owner::acknowledge(control).await?;
            // No await separates sending G from selecting owner-only teardown.
            driver.supervised = true;
        }
        let initialized = driver.request("initialize", json!({"clientInfo":{"name":"doxa","title":null,"version":env!("CARGO_PKG_VERSION")},"capabilities":{"experimentalApi":true}})).await?;
        driver.live_auto_supported = initialized["userAgent"].as_str().is_some_and(|agent|
            agent.starts_with(crate::codex_compact::PROTECTED_AGENT_PREFIX) && agent.contains("; doxa-midturn-auto-v1; "));
        driver.send(json!({"method":"initialized"})).await?;
        if driver.compact_gate.is_some() {
            // The server's build version is authoritative. The client version
            // in its suffix never becomes proof of the provider hook contract.
            let version = initialized["userAgent"].as_str().and_then(provider_version);
            if version != Some(crate::codex_compact::SUPPORTED_VERSION) {
                return Err(AppServerError::Protocol("Codex build has no verified DOXA compaction hook contract"));
            }
            if !initialized["userAgent"].as_str().is_some_and(|agent| agent.starts_with(
                crate::codex_compact::PROTECTED_AGENT_PREFIX)) {
                return Err(AppServerError::Protocol("Protected Codex requires DOXA's installed fail-closed app server; run scripts/install_codex_protected.py or select its launcher with --codex-bin"));
            }
            // Token-budget context resets bypass PreCompact in this provider.
            // Confirm the process-local override is supported and effective.
            let config = driver.request("config/read", json!({"cwd":driver.options.cwd,"includeLayers":false})).await?;
            if config["config"]["features"]["token_budget"] != false {
                return Err(AppServerError::Protocol("Codex unhooked token-budget reset path is not disabled"));
            }
            let hooks = driver.request("hooks/list", json!({"cwds":[driver.options.cwd]})).await?;
            driver.compact_gate.as_mut().unwrap().verify_hooks(&hooks)
                .map_err(|_| AppServerError::Protocol("DOXA Codex compaction hook is not active with verified trust"))?;
        }
        Ok(driver)
    }

    async fn start_thread(&mut self) -> Result<(), AppServerError> {
        let driver = self;
        let approval = if driver.interactive { driver.options.permission.approval_policy() } else { "never" };
        let sandbox = if driver.interactive { driver.options.permission.sandbox(driver.options.sandbox) } else { driver.options.sandbox };
        // Codex reserves `mcp` and `mcp__*` for provider MCP tools. Keep
        // canonical host names while registering aliases only at this boundary.
        // Primary contract: codex app-server thread_processor::validate_dynamic_tools.
        let mut tools = if driver.peer_tools { crate::peer_tools::definitions() } else { Vec::new() };
        tools.extend(driver.agent_tools.clone());
        driver.dynamic_tool_names.clear();
        for tool in &mut tools {
            let canonical = tool["name"].as_str().ok_or(AppServerError::Protocol("Dynamic tool lacks canonical name"))?.to_owned();
            let suffix = canonical.strip_prefix("mcp__doxa__")
                .ok_or(AppServerError::Protocol("Invalid canonical dynamic tool name"))?;
            let alias = format!("doxa_{suffix}");
            if alias.len() > 128 || !alias.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
                || driver.dynamic_tool_names.iter().any(|(name, _)| name == &alias) {
                return Err(AppServerError::Protocol("Invalid or duplicate Codex dynamic tool alias"));
            }
            tool["name"] = json!(alias);
            driver.dynamic_tool_names.push((alias, canonical));
        }
        let result = if let Some(id) = driver.options.resume_thread.clone() {
            let mut params=json!({"threadId":id,"cwd":driver.options.cwd,"model":driver.options.model,"approvalPolicy":approval,"sandbox":sandbox_name(sandbox),"excludeTurns":true});
            if let Some(path)=doxa_isolation::resume_rollout().map_err(AppServerError::Io)? {params["path"]=json!(path);}
            driver.request("thread/resume",params).await?
        } else {
            let mut params = json!({"cwd":driver.options.cwd,"model":driver.options.model,"approvalPolicy":approval,"sandbox":sandbox_name(sandbox)});
            if !tools.is_empty() { params["dynamicTools"] = json!(tools); }
            driver.request("thread/start", params).await?
        };
        // Protected sessions must establish the requested price/model basis
        // before any turn can run. Never accept a provider fallback silently.
        if driver.compact_gate.is_some() && driver.options.model.as_deref()
            .is_some_and(|expected| result["model"].as_str() != Some(expected)) {
            return Err(AppServerError::Protocol("Codex returned a different or unverified requested model; no turn started"));
        }
        let id = result.pointer("/thread/id").and_then(Value::as_str)
            .filter(|id| valid_thread_id(id))
            .ok_or(AppServerError::Protocol("thread response lacks a valid ID"))?;
        if driver.options.resume_thread.as_deref().is_some_and(|expected| expected != id) {
            return Err(AppServerError::Protocol("resume returned a different thread ID"));
        }
        driver.thread_id = Some(id.to_owned());
        if let Some(gate) = driver.compact_gate.as_mut() {
            gate.bind_thread(id).map_err(|_| AppServerError::Protocol("DOXA compaction gate could not bind the actual thread"))?;
        }
        // The account catalog default may differ from this thread's profile.
        // Only the thread/start or thread/resume response identifies its model.
        if let Some(model) = result["model"].as_str().filter(|value|
            !value.is_empty() && value.len() <= 128 && !value.chars().any(char::is_control)) {
            driver.effective_model = Some(model.to_owned());
            driver.options.model = Some(model.to_owned());
        }
        Ok(())
    }

    /// Catalog discovery initializes a short-lived process without creating a thread.
    pub async fn discover_models(options: AppServerOptions) -> Result<Vec<Value>, AppServerError> {
        let mut driver = Self::initialize(options, str::to_owned).await?;
        let result = driver.list_models().await;
        driver.shutdown().await;
        result
    }

    /// Read only the documented Codex subscription bucket. API-key sessions
    /// and malformed or unrelated buckets remain unknown.
    pub async fn read_rate_limits(&mut self) -> Option<Value> {
        let reply = self.request("account/rateLimits/read", json!({})).await.ok()?;
        codex_billing(&reply)
    }

    pub async fn list_models(&mut self) -> Result<Vec<Value>, AppServerError> {
        let mut rows = Vec::new();
        let mut cursor = Value::Null;
        for _ in 0..4 {
            let result = self.request("model/list", json!({"cursor":cursor,"limit":100,"includeHidden":false})).await?;
            for row in result["data"].as_array().ok_or(AppServerError::Protocol("invalid model catalog"))? {
                let Some(model) = row["model"].as_str().filter(|value| !value.is_empty() && value.len() <= 128 && !value.chars().any(char::is_control)) else { continue; };
                if row["hidden"] == true { continue; }
                let efforts: Vec<_> = row["supportedReasoningEfforts"].as_array().into_iter().flatten()
                    .filter_map(|value| value["reasoningEffort"].as_str())
                    .filter(|value| !value.is_empty() && value.len() <= 32 && value.bytes().all(|b| b.is_ascii_alphanumeric()))
                    .take(16).collect();
                let default = row["defaultReasoningEffort"].as_str().filter(|value| efforts.contains(value));
                rows.push(json!({"model":model,"efforts":efforts,"default_effort":default,"is_default":row["isDefault"] == true}));
                if rows.len() >= 100 { return Ok(rows); }
            }
            cursor = result["nextCursor"].clone();
            if cursor.is_null() { break; }
            if cursor.as_str().is_none_or(|value| value.len() > 1024) { return Err(AppServerError::Protocol("invalid catalog cursor")); }
        }
        Ok(rows)
    }

    pub fn set_selection(&mut self, model: Option<String>, effort: Option<String>) {
        if model != self.options.model { self.effective_model = None; }
        self.options.model = model;
        self.effort = effort;
    }

    pub fn set_permission(&mut self, permission: CodexPermission) {
        self.options.permission = permission;
    }

    pub fn thread_id(&self) -> &str { self.thread_id.as_deref().expect("thread start succeeded") }

    pub fn model(&self) -> Option<&str> { self.effective_model.as_deref() }

    pub async fn shutdown(&mut self) {
        self.kill_group();
        if !self.supervised { let _ = self.child.start_kill(); }
        let _ = timeout(Duration::from_secs(5), self.child.wait()).await;
    }

    fn kill_group(&mut self) {
        if self.supervised { self.owner_control.take(); return; }
        if let Some(pid) = self.process_group.take() {
            unsafe { libc::kill(-(pid as i32), libc::SIGKILL); }
        }
    }

    /// The caller must persist this ID *before* submitting a turn. The
    /// daemon integration will retain the existing incomplete-turn guard.
    pub async fn run_turn(
        &mut self, prompt: &str, cancel: &CancellationToken,
        emit: impl FnMut(EngineEvent),
    ) -> Result<(), AppServerError> {
        self.run_turn_interactive(prompt, cancel, emit, |_| Ok(None)).await
    }

    /// A host creates a single-use input receiver before the display event is
    /// emitted. Waiting for a user remains cancellable and time bounded.
    pub async fn run_turn_interactive(
        &mut self, prompt: &str, cancel: &CancellationToken,
        emit: impl FnMut(EngineEvent),
        request: impl FnMut(&Value) -> Result<Option<(EngineEvent, tokio::sync::oneshot::Receiver<Value>)>, String>,
    ) -> Result<(), AppServerError> {
        self.run_turn_controlled(prompt, cancel, emit, request, None).await
    }

    /// Only the private provider can acknowledge the live tightening. Other
    /// mode and sandbox changes remain idle operations.
    pub async fn run_turn_controlled(
        &mut self, prompt: &str, cancel: &CancellationToken,
        mut emit: impl FnMut(EngineEvent),
        mut request: impl FnMut(&Value) -> Result<Option<(EngineEvent, tokio::sync::oneshot::Receiver<Value>)>, String>,
        mut control: Option<&mut tokio::sync::mpsc::Receiver<LiveAutoRequest>>,
    ) -> Result<(), AppServerError> {
        if prompt.split_whitespace().next() == Some("/compact") {
            return Err(AppServerError::Protocol("Use the reviewed compaction operation; slash compaction cannot pass through a provider turn"));
        }
        if cancel.is_cancelled() { return Err(AppServerError::Cancelled); }
        self.reasoning_bytes = 0;
        self.reasoning_chars = 0;
        self.reasoning_buffer.clear();
        self.reasoning_truncated = false;
        self.assistant_buffers.clear();
        self.assistant_bytes = 0;
        self.assistant_message_emitted = false;
        self.usage = None;
        self.review_items.clear();
        self.tool_normalizer.begin_turn();
        let deadline = tokio::time::Instant::now() + self.options.turn_timeout;
        let thread_id = self.thread_id().to_owned();
        let permission = self.options.permission;
        let sandbox = permission.sandbox(self.options.sandbox);
        let request_id = self.send_request_bounded("turn/start", json!({"threadId":thread_id,"model":self.options.model,"effort":self.effort,
            "approvalPolicy":if self.interactive { permission.approval_policy() } else { "never" },
            "sandboxPolicy":{"type":sandbox_policy_type(sandbox)},
            "input":[{"type":"text","text":prompt,"text_elements":[]}]}), Some(cancel), deadline).await?;
        let response = self.wait_response(request_id, Some(cancel), deadline, true).await?;
        let turn_id = response.pointer("/turn/id").and_then(Value::as_str)
            .filter(|id| valid_thread_id(id))
            .ok_or(AppServerError::Protocol("turn response lacks a valid ID"))?.to_owned();
        self.turn_id = Some(turn_id.clone());
        let mut automatic_compaction_reviewed = false;
        loop {
            let frame = if let Some((frame, bytes)) = self.pending_notifications.pop_front() {
                self.pending_bytes = self.pending_bytes.saturating_sub(bytes);
                frame
            } else {
                tokio::select! {
                    biased;
                    Some(change) = next_live_auto(&mut control) => {
                        self.apply_live_auto(change, cancel, deadline).await?;
                        continue;
                    }
                    value = self.read_frame() => value?,
                    _ = cancel.cancelled() => {
                        let _ = self.send_request_bounded("turn/interrupt", json!({"threadId":thread_id,"turnId":turn_id}), None, tokio::time::Instant::now() + Duration::from_millis(200)).await;
                        return Err(AppServerError::Cancelled);
                    }
                    _ = tokio::time::sleep_until(deadline) => return Err(AppServerError::TimedOut),
                }
            };
            if let Some(method) = frame.get("method").and_then(Value::as_str) {
                if frame.get("id").is_some() {
                    let params = &frame["params"];
                    if params["threadId"].as_str() != Some(thread_id.as_str())
                        || params["turnId"].as_str() != Some(turn_id.as_str()) {
                        self.send_bounded(json!({"id":frame["id"],"error":{"code":-32602,"message":"Request does not belong to the active turn"}}), Some(cancel), deadline).await?;
                        return Err(AppServerError::Protocol("server request has a different thread or turn"));
                    }
                    let mut frame = frame;
                    if frame["method"] == "item/tool/call" {
                        let canonical = frame["params"]["tool"].as_str()
                            .and_then(|name| self.dynamic_tool_names.iter().find(|(alias, _)| alias == name))
                            .map(|(_, canonical)| canonical.clone());
                        if !frame["params"]["namespace"].is_null() || canonical.is_none() {
                            self.send_bounded(json!({"id":frame["id"],"error":{"code":-32602,"message":"Unregistered Codex dynamic tool"}}), Some(cancel), deadline).await?;
                            return Err(AppServerError::Protocol("Unregistered Codex dynamic tool"));
                        }
                        frame["params"]["tool"] = json!(canonical.unwrap());
                    }
                    if let Some(item) = self.review_items.iter().find(|item| item["id"] == frame["params"]["itemId"]) {
                        frame["doxa_item"] = item.clone();
                    }
                    if self.options.permission == CodexPermission::Auto && provider_approval(&frame["method"]) {
                        self.decline_auto_approval(&frame, cancel, deadline).await?;
                        continue;
                    }
                    let pending = request(&frame).map_err(|message| AppServerError::Server((self.scrub)(&message)))?;
                    if let Some((event, mut receiver)) = pending {
                        let request_id = event.data["id"].clone();
                        let is_peer = frame["method"] == "item/tool/call";
                        if is_peer {
                            let input = (self.scrub)(&frame["params"]["arguments"].to_string());
                            emit(EngineEvent::new("tool_call", json!({"id":frame["params"]["callId"],"name":frame["params"]["tool"],"input":input})));
                        }
                        emit(event);
                        let mut deny_after_switch = false;
                        let answer = loop { tokio::select! {
                            biased;
                            _ = cancel.cancelled() => break Err(AppServerError::Cancelled),
                            _ = tokio::time::sleep_until(deadline) => break Err(AppServerError::TimedOut),
                            Some(change) = next_live_auto(&mut control) => {
                                if self.apply_live_auto(change, cancel, deadline).await? && provider_approval(&frame["method"]) {
                                    deny_after_switch = true;
                                    break Ok(json!(null));
                                }
                            }
                            value = &mut receiver => break value.map_err(|_| AppServerError::Server("Codex input request was closed".into())),
                        }};
                        drop(receiver);
                        emit(EngineEvent::new("needs_input_resolved", json!({"id":request_id})));
                        if deny_after_switch {
                            self.decline_auto_approval(&frame, cancel, deadline).await?;
                            continue;
                        }
                        let answer = answer?;
                        if is_peer {
                            emit(EngineEvent::new("tool_result", json!({"id":frame["params"]["callId"],"is_error":answer["success"] != true})));
                            for content in answer["contentItems"].as_array().into_iter().flatten() {
                                if let Some(text) = content["text"].as_str() {
                                    let clean = (self.scrub)(text);
                                    emit(EngineEvent::new("tool_result_detail", json!({"id":frame["params"]["callId"],"text":clean})));
                                }
                            }
                        }
                        self.send_bounded(json!({"id":frame["id"],"result":answer}), Some(cancel), deadline).await?;
                    } else {
                        self.deny_server_request(&frame, Some(cancel), deadline).await?;
                    }
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
                    "hook/started" if params["run"]["eventName"] == "preCompact" => {
                        automatic_compaction_reviewed = false;
                        emit(EngineEvent::new("lore_review_started", json!({"before":"compaction"})));
                    }
                    "hook/completed" => {
                        if let Some(gate) = self.compact_gate.as_mut() {
                            match gate.observe_completion(&params["run"]) {
                                crate::codex_compact::ReviewOutcome::Reviewed => {
                                    automatic_compaction_reviewed = true;
                                    emit(EngineEvent::new("lore_review_completed", json!({"before":"compaction"})));
                                },
                                crate::codex_compact::ReviewOutcome::Blocked => return Err(AppServerError::Server("LORE review blocked Codex compaction".into())),
                                crate::codex_compact::ReviewOutcome::Failed => { self.kill_group(); return Err(AppServerError::Protocol("Codex compaction review hook failed; protected session stopped")); }
                                crate::codex_compact::ReviewOutcome::Unrelated => {},
                            }
                        }
                    }
                    "model/rerouted" => {
                        // A reroute invalidates a single-model ceiling even if
                        // the replacement also happens to have a price row.
                        self.effective_model = None;
                        emit(EngineEvent::new("model_changed", json!({"model":null,"message":"Codex rerouted this turn; effective model accounting is unknown"})));
                    }
                    "item/agentMessage/delta" => {
                        if let (Some(id), Some(delta)) = (params["itemId"].as_str(), params["delta"].as_str()) {
                            if !valid_thread_id(id) { return Err(AppServerError::Protocol("invalid assistant item ID")); }
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
                            "inference_reasoning_output_tokens":last["reasoningOutputTokens"].as_u64(),
                            "context_window":context_window,"context_used":last["totalTokens"].as_u64(),
                            "reasoning_count_is_estimate":true
                        })));
                    }
                    "item/started" | "item/completed" => {
                        let item = &params["item"];
                        if item["type"] == "contextCompaction" && method == "item/completed"
                            && self.compact_gate.is_some() && !automatic_compaction_reviewed {
                            self.kill_group();
                            return Err(AppServerError::Protocol("automatic compaction completed without observed native LORE review; protected session stopped"));
                        }
                        if item["type"] == "contextCompaction" && method == "item/completed" {
                            automatic_compaction_reviewed = false;
                        }
                        if item["type"] == "fileChange" {
                            if let Some(index) = self.review_items.iter().position(|old| old["id"] == item["id"]) {
                                self.review_items.remove(index);
                            }
                            if self.review_items.len() < 32 && serde_json::to_vec(item).is_ok_and(|v| v.len() <= 16 * 1024) {
                                self.review_items.push(item.clone());
                            }
                        }
                        if method == "item/completed" && params["item"]["type"] == "agentMessage" {
                            if let Some(id) = params["item"]["id"].as_str() {
                                if !valid_thread_id(id) { return Err(AppServerError::Protocol("invalid assistant item ID")); }
                                let buffered = self.assistant_buffers.iter().position(|(key, _)| key == id)
                                    .map(|index| self.assistant_buffers.remove(index).1);
                                let buffered_bytes = buffered.as_ref().map_or(0, String::len);
                                let text = params["item"]["text"].as_str().map(str::to_owned).or(buffered).unwrap_or_default();
                                self.assistant_bytes = self.assistant_bytes.saturating_add(text.len().saturating_sub(buffered_bytes));
                                if self.assistant_bytes > MAX_FRAME_BYTES { return Err(AppServerError::Protocol("assistant turn text exceeded display limit")); }
                                self.emit_assistant_message(&text, &mut emit)?;
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
                        for (_, text) in std::mem::take(&mut self.assistant_buffers) {
                            self.emit_assistant_message(&text, &mut emit)?;
                        }
                        let total = self.usage.as_ref().map(|u| &u["total"]);
                        let last = self.usage.as_ref().map(|u| &u["last"]);
                        let window = self.usage.as_ref().and_then(|u| u["modelContextWindow"].as_u64()).and_then(|window| window.checked_sub(12_000)).filter(|window| *window > 0);
                        let used = last.and_then(|u| u["totalTokens"].as_u64()).map(|used| used.saturating_sub(12_000));
                        let pct = used.zip(window).and_then(|(used, window)|
                            (window > 0 && used <= window).then_some(100.0 * used as f64 / window as f64));
                        emit(EngineEvent::new("turn_done", json!({
                            "is_error":failed,"error":error,"usage_scope":"session",
                            "model":self.effective_model,
                            "model_consistent":self.effective_model.is_some() && self.effective_model == self.options.model,
                            "usage_complete":total.is_some_and(|u| u["inputTokens"].as_u64().is_some() && u["outputTokens"].as_u64().is_some()),
                            "usage_source":"codex_app_server_token_usage_updated",
                            "input_tokens":total.and_then(|u| u["inputTokens"].as_u64()),
                            "output_tokens":total.and_then(|u| u["outputTokens"].as_u64()),
                            "cache_read_input_tokens":total.and_then(|u| u["cachedInputTokens"].as_u64()),
                            "reasoning_output_tokens":null,"reasoning_count_is_estimate":true,
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

    async fn apply_live_auto(&mut self, change: LiveAutoRequest, cancel: &CancellationToken,
        turn_deadline: tokio::time::Instant) -> Result<bool, AppServerError> {
        let refusal = if change.expires <= std::time::Instant::now() {
            Some("Codex auto request expired before application")
        } else if !self.live_auto_supported {
            Some("Codex mid-turn auto requires an updated protected provider; rebuild with scripts/install_codex_protected.py")
        } else if self.options.permission != CodexPermission::OnRequest
            || self.options.sandbox == SandboxMode::DangerFullAccess {
            Some("This Codex permission or sandbox transition requires an idle session")
        } else { None };
        if let Some(reason) = refusal { let _ = change.reply.send(Err(reason)); return Ok(false); }
        let deadline = turn_deadline.min(tokio::time::Instant::now() + Duration::from_secs(8));
        let id = self.send_request_bounded("turn/settings/update", json!({
            "threadId":self.thread_id(),"turnId":self.turn_id,"doxaAuto":true
        }), Some(cancel), deadline).await?;
        match self.wait_response(id, Some(cancel), deadline, true).await {
            Ok(result) if result["status"] == "applied" => {
                self.options.permission = CodexPermission::Auto;
                let _ = change.reply.send(Ok(self.thread_id().to_owned()));
                Ok(true)
            }
            Ok(_) | Err(AppServerError::Server(_)) => {
                let _ = change.reply.send(Err("Codex provider did not apply the active auto policy"));
                Ok(false)
            }
            Err(error) => {
                let _ = change.reply.send(Err("Codex live permission update could not be verified"));
                Err(error)
            }
        }
    }

    fn emit_assistant_message(&mut self, text: &str, emit: &mut impl FnMut(EngineEvent)) -> Result<(), AppServerError> {
        if text.is_empty() { return Ok(()); }
        // Scrub the whole provider message before adding a display separator;
        // fragment boundaries never become secret-scrubbing boundaries.
        let clean = (self.scrub)(text);
        if clean.is_empty() { return Ok(()); }
        let separator = if self.assistant_message_emitted { "\n\n" } else { "" };
        self.assistant_bytes = self.assistant_bytes.saturating_add(separator.len());
        if self.assistant_bytes > MAX_FRAME_BYTES {
            return Err(AppServerError::Protocol("assistant turn text exceeded display limit"));
        }
        emit(EngineEvent::new("text_delta", json!({"text":format!("{separator}{clean}")})));
        self.assistant_message_emitted = true;
        Ok(())
    }

    async fn request(&mut self, method: &str, params: Value) -> Result<Value, AppServerError> {
        let id = self.send_request(method, params).await?;
        timeout(RPC_TIMEOUT, self.wait_response(id, None, tokio::time::Instant::now() + RPC_TIMEOUT, false))
            .await.map_err(|_| AppServerError::TimedOut)?
    }

    async fn send_request(&mut self, method: &str, params: Value) -> Result<u64, AppServerError> {
        self.send_request_bounded(method, params, None, tokio::time::Instant::now() + RPC_TIMEOUT).await
    }

    async fn send_request_bounded(&mut self, method: &str, params: Value, cancel: Option<&CancellationToken>, deadline: tokio::time::Instant) -> Result<u64, AppServerError> {
        self.next_id += 1;
        self.send_bounded(json!({"id":self.next_id,"method":method,"params":params}), cancel, deadline).await?;
        Ok(self.next_id)
    }

    async fn send(&mut self, frame: Value) -> Result<(), AppServerError> {
        self.send_bounded(frame, None, tokio::time::Instant::now() + RPC_TIMEOUT).await
    }

    async fn send_bounded(&mut self, frame: Value, cancel: Option<&CancellationToken>, deadline: tokio::time::Instant) -> Result<(), AppServerError> {
        if cancel.is_some_and(CancellationToken::is_cancelled) { return Err(AppServerError::Cancelled); }
        let frame = doxa_isolation::map_frame(frame)?;
        let mut encoded = serde_json::to_vec(&frame).map_err(io::Error::other)?;
        if encoded.len() > MAX_FRAME_BYTES { return Err(AppServerError::Protocol("outgoing frame too large")); }
        encoded.push(b'\n');
        tokio::select! {
            biased;
            _ = async { if let Some(token) = cancel { token.cancelled().await } else { std::future::pending().await } } => Err(AppServerError::Cancelled),
            _ = tokio::time::sleep_until(deadline) => Err(AppServerError::TimedOut),
            result = async { self.stdin.write_all(&encoded).await?; self.stdin.flush().await } => result.map_err(AppServerError::Io),
        }
    }

    async fn read_frame(&mut self) -> Result<Value, AppServerError> {
        // A control message can interrupt this read after partial bytes have
        // arrived. Retain them across dropped read futures.
        loop {
            let available = self.stdout.fill_buf().await?;
            if available.is_empty() { return Err(AppServerError::Protocol("app-server closed stdout")); }
            let take = available.iter().position(|byte| *byte == b'\n').map_or(available.len(), |at| at + 1);
            if self.frame_buffer.len().saturating_add(take) > MAX_FRAME_BYTES {
                return Err(AppServerError::Protocol("app-server frame too large"));
            }
            let done = available[take - 1] == b'\n';
            self.frame_buffer.extend_from_slice(&available[..take]);
            self.stdout.consume(take);
            if done { break; }
        }
        serde_json::from_slice(&std::mem::take(&mut self.frame_buffer)).map_err(|_| AppServerError::Protocol("invalid app-server JSON frame"))
    }

    async fn wait_response(&mut self, id: u64, cancel: Option<&CancellationToken>, deadline: tokio::time::Instant, queue_requests: bool) -> Result<Value, AppServerError> {
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
            if frame.get("method").is_some() && frame.get("id").is_some() && !queue_requests {
                self.deny_server_request(&frame, cancel, deadline).await?;
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

    /// Auto refusals are tool results: the model can retry within its sandbox.
    async fn decline_auto_approval(&mut self, frame: &Value, cancel: &CancellationToken,
        deadline: tokio::time::Instant) -> Result<(), AppServerError> {
        let result = match frame["method"].as_str() {
            Some("item/commandExecution/requestApproval" | "item/fileChange/requestApproval") => json!({"decision":"decline"}),
            Some("item/permissions/requestApproval") => json!({"permissions":{},"scope":"turn"}),
            _ => return Err(AppServerError::Protocol("invalid automatic approval refusal")),
        };
        self.send_bounded(json!({"id":frame["id"],"result":result}), Some(cancel), deadline).await
    }

    /// Noninteractive sessions explicitly deny provider approval requests.
    /// Unknown requests remain unsupported and fail closed.
    async fn deny_server_request(&mut self, frame: &Value, cancel: Option<&CancellationToken>, deadline: tokio::time::Instant) -> Result<(), AppServerError> {
        let Some(id) = frame.get("id") else { return Ok(()); };
        let result = match frame["method"].as_str().unwrap_or("") {
            "item/commandExecution/requestApproval" => json!({"decision":"decline"}),
            "item/fileChange/requestApproval" => json!({"decision":"decline"}),
            "item/permissions/requestApproval" => json!({"permissions":{},"scope":"turn"}),
            _ => {
                self.send_bounded(json!({"id":id,"error":{"code":-32601,"message":"DOXA cannot handle this server request"}}), cancel, deadline).await?;
                return Err(AppServerError::Server("Codex requested an interactive tool or approval that DOXA cannot handle; the request was refused".to_owned()));
            }
        };
        self.send_bounded(json!({"id":id,"result":result}), cancel, deadline).await?;
        Err(AppServerError::Server("Codex requested interactive approval; DOXA refused it because this session has no approval bridge".to_owned()))
    }
}

impl Drop for AppServerDriver {
    fn drop(&mut self) {
        self.kill_group();
        if !self.supervised { let _ = self.child.start_kill(); }
    }
}

fn sandbox_name(mode: SandboxMode) -> &'static str {
    match mode {
        SandboxMode::ReadOnly => "read-only",
        SandboxMode::WorkspaceWrite => "workspace-write",
        SandboxMode::DangerFullAccess => "danger-full-access",
    }
}

fn sandbox_policy_type(mode: SandboxMode) -> &'static str {
    match mode {
        SandboxMode::ReadOnly => "readOnly",
        SandboxMode::WorkspaceWrite => "workspaceWrite",
        SandboxMode::DangerFullAccess => "dangerFullAccess",
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
            "action":item["action"],"status":item["status"]
        }),
        _ => return None,
    };
    Some(value)
}

#[cfg(test)]
mod web_item_tests {
    use super::*;

    #[test]
    fn auto_cannot_inherit_an_unrestricted_configured_sandbox() {
        assert_eq!(CodexPermission::Auto.sandbox(SandboxMode::DangerFullAccess), SandboxMode::WorkspaceWrite);
        assert_eq!(CodexPermission::Auto.approval_policy(), "never");
        assert_eq!(CodexPermission::FullAccess.sandbox(SandboxMode::ReadOnly), SandboxMode::DangerFullAccess);
    }

    #[test]
    fn billing_uses_only_measured_codex_windows() {
        let reply = json!({"rateLimitsByLimitId":{"codex":{"limitId":"codex",
            "planType":"plus","primary":{"usedPercent":67.2,"windowDurationMins":300},
            "secondary":{"usedPercent":91,"windowDurationMins":10080}},
            "codex_other":{"limitId":"codex_other","primary":{"usedPercent":100,"windowDurationMins":60}}}});
        assert_eq!(codex_billing(&reply), Some(json!({"mode":"subscription","type":"plus","quota":"5h:67.2% · week:91%"})));
        assert_eq!(codex_billing(&json!({"rateLimits":{"limitId":"codex_other","primary":{"usedPercent":20,"windowDurationMins":300}}})), None);
        assert_eq!(codex_billing(&json!({"rateLimits":{"limitId":"codex","primary":{"usedPercent":101,"windowDurationMins":300}}})), None);
    }

    #[test]
    fn provider_version_uses_only_the_authoritative_product_prefix() {
        let desktop = "Codex Desktop/0.156.1 (Ubuntu 26.4.0; x86_64) dumb (doxa; 2.0.0-alpha.37)";
        assert_eq!(provider_version(desktop), Some(crate::codex_compact::SUPPORTED_VERSION));
        assert_eq!(provider_version("codex_cli_rs/0.156.1"), Some(crate::codex_compact::SUPPORTED_VERSION));
        for agent in [
            "Codex Desktop/0.1.0 (doxa; 0.156.1)",
            "codex_cli_rs/0.1.0 (client/0.156.1)",
            "Codex Desktop (doxa/0.156.1)",
            "codex_cli_rs (doxa; 0.156.1)",
            "unknown/0.156.1 (doxa; 0.156.1)",
            "doxa/0.156.1",
            "Codex Desktop/unknown (doxa; 0.156.1)",
            "Codex Desktop/0.156.1-client",
        ] {
            assert_ne!(provider_version(agent), Some(crate::codex_compact::SUPPORTED_VERSION), "{agent}");
        }
    }

    /// Opt-in installed-provider probe: initialization and hook inventory only.
    #[tokio::test]
    #[ignore = "requires DOXA_CODEX_PROBE pointing to installed Codex 0.156.1"]
    async fn installed_codex_hook_inventory_verifies_without_starting_a_thread() {
        use std::os::unix::fs::PermissionsExt;
        let executable = PathBuf::from(std::env::var_os("DOXA_CODEX_PROBE").expect("explicit installed provider"));
        let scratch = PathBuf::from(std::env::var_os("TMPDIR").expect("real disk scratch"));
        let dir = tempfile::tempdir_in(scratch).unwrap();
        let home = dir.path().join("home");
        let gate_dir = dir.path().join("gate");
        let codex_home = dir.path().join("codex-home");
        for path in [&home, &gate_dir, &codex_home] {
            std::fs::create_dir(path).unwrap();
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        // Run this ignored probe alone: no account settings or credentials are
        // inherited from the normal user config directory.
        std::env::set_var("HOME", &home);
        std::env::set_var("CODEX_HOME", &codex_home);
        std::env::remove_var("OPENAI_API_KEY");
        let gate = crate::codex_compact::CompactGate::prepare(&gate_dir,
            &std::env::current_exe().unwrap(), &codex_home, dir.path(), "installed-probe",
            crate::codex_compact::SUPPORTED_VERSION).unwrap();
        let options = AppServerOptions { executable, cwd: dir.path().to_owned(), model: None,
            sandbox: SandboxMode::WorkspaceWrite, permission: CodexPermission::OnRequest, resume_thread: None, turn_timeout: Duration::from_secs(5) };
        let mut driver = timeout(Duration::from_secs(20),
            AppServerDriver::initialize_with_gate(options, str::to_owned, Some(gate))).await.unwrap().unwrap();
        assert!(driver.thread_id.is_none(), "inventory probe must never start a provider thread");
        driver.shutdown().await;
    }

    #[test]
    fn generated_codex_web_search_actions_survive_adapter_and_completion() {
        let mut normalizer = CodexJsonlNormalizer::new(|text| text.replace("fixture-secret","[redacted]"));
        let start = normalize_tool_item(&json!({"type":"webSearch","id":"web_1","query":"","action":null})).unwrap();
        let events = normalizer.push_bytes(format!("{}\n",json!({"type":"item.started","item":start})).as_bytes()).unwrap();
        assert!(events[0].data["input"].get("query").is_none());
        let item = normalize_tool_item(&json!({"type":"webSearch","id":"web_1","query":"",
            "action":{"type":"search","queries":["weather fixture-secret","forecast today"]}})).unwrap();
        let events = normalizer.push_bytes(format!("{}\n",json!({"type":"item.completed","item":item})).as_bytes()).unwrap();
        assert_eq!(events[0].kind,"tool_result");
        assert_eq!(events[0].data["input"]["queries"],json!(["weather [redacted]","forecast today"]));
        assert!(events[0].data["result_summary"].as_str().unwrap().contains("not exposed by Codex"));
        assert!(!serde_json::to_string(&events.iter().map(|event|&event.data).collect::<Vec<_>>()).unwrap().contains("fixture-secret"));
        for action in [json!({"type":"openPage","url":"https://example.com"}),
            json!({"type":"findInPage","url":"https://example.com","pattern":"forecast"})] {
            let item = normalize_tool_item(&json!({"type":"webSearch","id":"web_2","action":action})).unwrap();
            let events = normalizer.push_bytes(format!("{}\n",json!({"type":"item.completed","item":item})).as_bytes()).unwrap();
            assert_eq!(events[0].data["input"]["url"],"https://example.com");
        }
    }
}

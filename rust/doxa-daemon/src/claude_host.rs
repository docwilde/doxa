//! Native Claude Code host: one CLI owner, immediate stream events, exact control replies.
use doxa_claude::{Cli, CliOptions, Error};
use doxa_lore::{stream::StreamScrubber, LoreClient};
use doxa_runtime::{Host, PeerToolHandler};
use doxa_transcript::TranscriptStore;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, Sender, SyncSender},
        Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
const POLL: Duration = Duration::from_millis(20);
const RPC_TIMEOUT: Duration = Duration::from_secs(60);
type Reply = Sender<Result<Value, String>>;
enum Command {
    Prompt(String, SyncSender<Value>, Reply),
    Rpc(String, Value, Reply),
    Close,
}
struct Shared {
    session: String,
    cwd: String,
    lore: Mutex<LoreClient>,
    store: TranscriptStore,
    enabled: bool,
    failed: AtomicBool,
    active: AtomicBool,
    closing: AtomicBool,
    cancelled: AtomicBool,
    resume_identity_pending: AtomicBool,
    selection: Mutex<(Option<String>, Option<String>, String)>,
    account: Mutex<Option<Value>>,
    billing: Mutex<Option<Value>>,
    quota_limits: Mutex<Value>,
    catalog: Mutex<Value>,
    agent: Option<Arc<crate::agent_tools::AgentTools>>,
    peer: Mutex<Option<PeerToolHandler>>,
    session_tools: Mutex<Option<PeerToolHandler>>,
    peer_allowed: bool,
    depth: u32,
    parent: Option<String>,
}
impl Shared {
    fn scrub(&self, text: &str) -> Result<String, String> {
        self.lore
            .lock()
            .map_err(|_| "LORE scrub unavailable")?
            .scrub(text)
            .map_err(|_| {
                self.failed.store(true, Ordering::Release);
                "LORE scrub failed; Claude data withheld".into()
            })
    }
    fn clean(&self, value: &Value) -> Result<Value, String> {
        match value {
            Value::String(s) => Ok(json!(self.scrub(s)?)),
            Value::Array(a) => a
                .iter()
                .map(|v| self.clean(v))
                .collect::<Result<Vec<_>, _>>()
                .map(Value::Array),
            Value::Object(o) => o
                .iter()
                .map(|(k, v)| Ok((k.clone(), self.clean(v)?)))
                .collect::<Result<serde_json::Map<_, _>, String>>()
                .map(Value::Object),
            v => Ok(v.clone()),
        }
    }
    fn persist(&self, record: Value) -> Result<(), String> {
        self.store
            .try_append(record, "claude", |s| {
                self.scrub(s).map_err(io::Error::other)
            })
            .map_err(|_| {
                self.failed.store(true, Ordering::Release);
                "Claude transcript write failed".into()
            })
    }
    fn checkpoint(&self, incomplete: bool) -> Result<(), String> {
        let s = self.selection.lock().unwrap();
        let fields = json!({"thread_id":self.session,"session_id":self.session,"engine":"claude","transport":"stream-json","cwd":self.cwd,"model":s.0,"effort":s.1,"permission_mode":s.2,"lore_enabled":self.enabled,"lore_tools":self.agent.is_some(),"spawn_depth":self.depth,"parent_session_id":self.parent,"turn_incomplete":incomplete,"recorded":crate::iso_now()});
        self.store
            .try_write_thread(fields.as_object().unwrap().clone(), |s| {
                self.scrub(s).map_err(io::Error::other)
            })
            .map_err(|_| {
                self.failed.store(true, Ordering::Release);
                "Claude checkpoint write failed".into()
            })
    }
    fn review(&self, older: bool) -> bool {
        if !self.enabled || self.failed.load(Ordering::Acquire) || doxa_lore::review_disabled().unwrap_or(true) {
            return false;
        }
        let metadata = json!({"cwd":self.cwd,"session_id":self.session,"transcript":self.store.transcript_path(),"older":older});
        std::env::current_exe().ok().is_some_and(|exe| {
            doxa_engines::review_worker::review(
                &exe,
                &metadata,
                "claude",
                Duration::from_secs(180),
                || {
                    older
                        && (self.closing.load(Ordering::Acquire)
                            || self.cancelled.load(Ordering::Acquire))
                },
            )
            .unwrap_or(false)
        })
    }
    fn tools(&self) -> Vec<Value> {
        let mut rows = self
            .agent
            .as_ref()
            .map(|a| a.definitions())
            .unwrap_or_default();
        if self.session_tools.lock().is_ok_and(|h| h.is_some()) {
            rows.extend(doxa_engines::session_tools::definitions());
        }
        if self.peer_allowed {
            rows.extend(doxa_engines::peer_tools::definitions());
        }
        rows.into_iter().map(|r|json!({"name":r["name"].as_str().unwrap().strip_prefix("mcp__doxa__").unwrap(),"description":r["description"],"inputSchema":r["inputSchema"]})).collect()
    }
    fn tool(&self, name: &str, args: &Value) -> Result<Value, String> {
        let wire = format!("mcp__doxa__{name}");
        if wire == doxa_engines::session_tools::SPAWN {
            return (self
                .session_tools
                .lock()
                .map_err(|_| "Session tools unavailable")?
                .clone()
                .ok_or("Session tools unavailable")?)(&wire, args);
        }
        if let Some(agent) = &self.agent {
            if agent.contains(&wire) {
                return agent.call(&wire, args);
            }
        }
        let (method, normalized) = doxa_engines::peer_tools::validated_call(&wire, args).map_err(str::to_owned)?;
        (self
            .peer
            .lock()
            .map_err(|_| "peer tools unavailable")?
            .clone()
            .ok_or("peer tools unavailable")?)(method, &normalized)
    }
}
pub struct ClaudeHost {
    commands: Sender<Command>,
    worker: Mutex<Option<JoinHandle<()>>>,
    shared: Arc<Shared>,
    admission: Mutex<()>,
}
fn init_request() -> Value {
    json!({"subtype":"initialize","sdkMcpServers":["doxa"],"hooks":{"PreCompact":[{"hookCallbackIds":["doxa_pre_compact"],"timeout":200}],"UserPromptSubmit":[{"hookCallbackIds":["doxa_prompt_context"],"timeout":10}]}})
}
fn mcp_reply(request: &Value, shared: &Shared) -> Option<Value> {
    let msg = &request["message"];
    let id = msg.get("id")?;
    let result = match msg["method"].as_str()? {
        "initialize" => {
            json!({"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"doxa","version":env!("CARGO_PKG_VERSION")}})
        }
        "tools/list" => json!({"tools":shared.tools()}),
        _ => return None,
    };
    Some(json!({"mcp_response":{"jsonrpc":"2.0","id":id,"result":result}}))
}
fn startup_control(
    cli: &mut Cli,
    request: Value,
    shared: &Shared,
    deadline: Instant,
) -> Result<Value, String> {
    let id = cli
        .control(request)
        .map_err(|_| "Claude control write failed")?;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err("Claude initialization timed out".into());
        }
        match cli.recv(remaining.min(Duration::from_secs(1))) {
            Ok(frame)
                if frame["type"] == "control_response" && frame["response"]["request_id"] == id =>
            {
                return if frame["response"]["subtype"] == "success" {
                    Ok(frame["response"]["response"].clone())
                } else {
                    Err("Claude initialization control refused".into())
                }
            }
            Ok(frame) if frame["type"] == "control_request" => {
                let request = &frame["request"];
                let rid = frame["request_id"]
                    .as_str()
                    .ok_or("invalid Claude request")?;
                let response = if request["subtype"] == "mcp_message" {
                    mcp_reply(request,shared).unwrap_or_else(||json!({"mcp_response":{"jsonrpc":"2.0","id":request["message"]["id"],"result":{}}}))
                } else {
                    return Err("unexpected interactive Claude startup request".into());
                };
                cli.respond(rid, Ok(response))
                    .map_err(|_| "Claude startup reply failed")?;
            }
            Ok(frame) if frame["type"] == "rate_limit_event" => {
                for limit in rate_limits(&frame["rate_limit_info"]) {
                    let _ = update_quota(shared, &limit);
                }
            }
            Ok(frame) if frame["type"] == "system" => {}
            Ok(_) => return Err("unexpected Claude startup frame".into()),
            Err(Error::Timeout) => {}
            Err(_) => {
                return Err(
                    "Claude CLI closed during initialization; check doxa auth status claude".into(),
                )
            }
        }
    }
}
fn settings(value: &Value) -> Result<(Option<String>, Option<String>), String> {
    let model = value["applied"]["model"]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= 128 && !s.chars().any(char::is_control))
        .ok_or("Claude effective model unavailable")?;
    let effort = value["applied"]["effort"]
        .as_str()
        .filter(|s| matches!(*s, "low" | "medium" | "high" | "xhigh" | "max"))
        .map(str::to_owned);
    Ok((Some(model.into()), effort))
}
fn catalog(value: &Value) -> Result<Value, String> {
    let rows = value["models"]
        .as_array()
        .filter(|a| !a.is_empty() && a.len() <= 64)
        .ok_or("Claude model catalog unavailable")?;
    let mut ids = Vec::new();
    let mut capabilities = Vec::new();
    for row in rows {
        let name = row["value"]
            .as_str()
            .or_else(|| row["model"].as_str())
            .filter(|s| !s.is_empty() && s.len() <= 128 && !s.chars().any(char::is_control))
            .ok_or("invalid Claude model catalog")?;
        let mut names = vec![name];
        if let Some(resolved) = row["resolvedModel"]
            .as_str()
            .filter(|s| !s.is_empty() && s.len() <= 128 && !s.chars().any(char::is_control))
        {
            if resolved != name {
                names.push(resolved);
            }
        }
        let efforts = row
            .get("supportedEfforts")
            .or_else(|| row.get("supportedEffortLevels"))
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .filter(|s| matches!(*s, "low" | "medium" | "high" | "xhigh" | "max"))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        for name in names {
            if !ids.contains(&name) {
                ids.push(name);
                capabilities.push(json!({"model":name,"efforts":efforts}));
            }
        }
    }
    Ok(
        json!({"models":ids,"capabilities":capabilities,"note":"Provider catalog refreshed from the connected Claude CLI"}),
    )
}
impl ClaudeHost {
    pub fn new(
        executable: &Path,
        cwd: &Path,
        session_id: &str,
        resume: bool,
        model: Option<&str>,
        effort: Option<&str>,
        _runtime: &Path,
        depth: u32,
        parent: Option<&str>,
    ) -> Result<Self, String> {
        if !doxa_claude::cli::canonical_session_id(session_id) {
            return Err("Claude sessions require a canonical hyphenated UUID".into());
        }
        let mut lore = LoreClient::open(Duration::from_secs(5))
            .map_err(|_| "Native LORE scrub unavailable")?;
        lore.scrub("Claude scrub preflight")
            .map_err(|_| "Native LORE scrub unavailable")?;
        let cwd = cwd
            .to_str()
            .ok_or("Claude workspace must be UTF-8")?
            .to_owned();
        let (root, slug) = lore
            .transcript_identity(&cwd)
            .map_err(|_| "LORE transcript identity unavailable")?;
        let store = TranscriptStore::new(&root, &slug, session_id)
            .map_err(|_| "Claude transcript store unavailable")?;
        let enabled = doxa_state::lore_enabled_default();
        let mut requested_model = model.map(str::to_owned);
        let mut requested_effort = effort.map(str::to_owned);
        let mut mode = "default".to_owned();
        if resume {
            let old = match store
                .read_thread()
                .map_err(|_| "Claude saved state unavailable")?
            {
                Some(old) => old,
                None => {
                    if depth != 0 || parent.is_some() {
                        return Err("Legacy Claude lineage is unproven; restore refused".into());
                    }
                    doxa_claude::resume::verify_legacy(
                        &doxa_claude::isolation::config_dir(),
                        &store.transcript_path(),
                        session_id,
                        Path::new(&cwd),
                    )
                    .map_err(|error| format!("Legacy Claude restore refused: {error}"))?;
                    let fields = json!({"thread_id":session_id,"session_id":session_id,"engine":"claude","transport":"stream-json","cwd":cwd,"lore_enabled":enabled,"spawn_depth":0,"parent_session_id":null,"turn_incomplete":false,"legacy_imported":true,"permission_mode":"default"});
                    store
                        .try_write_thread(fields.as_object().unwrap().clone(), |s| {
                            lore.scrub(s).map_err(io::Error::other)
                        })
                        .map_err(|_| "Legacy Claude checkpoint failed")?;
                    store
                        .read_thread()
                        .map_err(|_| "Legacy Claude checkpoint unavailable")?
                        .ok_or("Legacy Claude checkpoint unavailable")?
                }
            };
            if old["engine"] != "claude"
                || old["thread_id"] != session_id
                || old["cwd"] != cwd
                || old["lore_enabled"] != enabled
                || old["spawn_depth"] != depth
                || old["parent_session_id"].as_str() != parent
                || old["turn_incomplete"] != false
            {
                return Err("Claude resume identity or durable transcript boundary changed; restore refused".into());
            }
            store
                .verify_thread_checkpoint(&old)
                .map_err(|_| "Claude durable transcript boundary changed")?;
            if requested_model.is_none() {
                requested_model = old["model"].as_str().map(str::to_owned)
            }
            if requested_effort.is_none() {
                requested_effort = old["effort"].as_str().map(str::to_owned)
            }
            mode = old["permission_mode"]
                .as_str()
                .unwrap_or("default")
                .to_owned();
        }
        let home = doxa_claude::isolation::prepare()
            .map_err(|_| "Claude private configuration unavailable")?;
        let plugins = doxa_claude::isolation::adopted_plugins()
            .map_err(|_| "Claude plugin adoption unavailable")?;
        let config = doxa_state::load_config(
            &doxa_claude::isolation::config_dir()
                .parent()
                .unwrap()
                .join("config.toml"),
        );
        let raw = doxa_state::raw_setting(
            std::env::var("DOXA_AGENT_PEER_SEND").ok().as_deref(),
            &config,
            "agent_peer_send",
        );
        let peer_allowed = !raw.is_empty()
            && !matches!(
                raw.to_ascii_lowercase().as_str(),
                "0" | "false" | "no" | "off"
            );
        let shared = Arc::new(Shared {
            session: session_id.into(),
            cwd: cwd.clone(),
            lore: Mutex::new(lore),
            store,
            enabled,
            failed: AtomicBool::new(false),
            active: AtomicBool::new(false),
            closing: AtomicBool::new(false),
            cancelled: AtomicBool::new(false),
            resume_identity_pending: AtomicBool::new(resume),
            selection: Mutex::new((
                requested_model.clone(),
                requested_effort.clone(),
                mode.clone(),
            )),
            account: Mutex::new(None),
            billing: Mutex::new(None),
            quota_limits: Mutex::new(json!({})),
            catalog: Mutex::new(Value::Null),
            agent: crate::agent_tools::AgentTools::new(&cwd, session_id, "claude", enabled),
            peer: Mutex::new(None),
            session_tools: Mutex::new(None),
            peer_allowed,
            depth,
            parent: parent.map(str::to_owned),
        });
        let mut cli = Cli::spawn(CliOptions {
            executable,
            cwd: Path::new(&cwd),
            session_id,
            resume,
            model: requested_model.as_deref(),
            effort: requested_effort.as_deref(),
            permission_mode: if mode == "default" { "manual" } else { &mode },
            config_dir: &home,
            plugins: &plugins,
        })
        .map_err(|_| "Claude CLI could not start")?;
        let deadline = Instant::now() + RPC_TIMEOUT;
        let initial = startup_control(&mut cli, init_request(), &shared, deadline)?;
        let effective = settings(&startup_control(
            &mut cli,
            json!({"subtype":"get_settings"}),
            &shared,
            deadline,
        )?)?;
        if requested_effort.is_some() && effective.1 != requested_effort {
            return Err("Claude did not apply requested startup effort".into());
        }
        let _ = catalog(&initial)?;
        *shared.catalog.lock().unwrap() = initial.clone();
        if let Some(requested) = requested_model.as_deref() {
            if !model_applied(&initial, requested, effective.0.as_deref()) {
                return Err("Claude did not apply requested startup model".into());
            }
        }
        *shared.selection.lock().unwrap() = (effective.0, effective.1, mode);
        *shared.account.lock().unwrap() = display_account(&initial["account"]);
        if let Some(account) = shared.account.lock().unwrap().as_ref() {
            set_billing_account(&shared, account);
        }
        let (tx, rx) = mpsc::channel();
        let owned = shared.clone();
        let worker = thread::spawn(move || broker(cli, rx, owned));
        Ok(Self {
            commands: tx,
            worker: Mutex::new(Some(worker)),
            shared,
            admission: Mutex::new(()),
        })
    }
    fn rpc(&self, method: &str, params: Value) -> Result<Value, String> {
        let (tx, rx) = mpsc::channel();
        self.commands
            .send(Command::Rpc(method.into(), params, tx))
            .map_err(|_| "Claude CLI closed")?;
        rx.recv_timeout(RPC_TIMEOUT)
            .map_err(|_| "Claude control timed out")?
    }
    pub fn shutdown(&self) -> bool {
        self.shared.closing.store(true, Ordering::Release);
        let _ = self.rpc("interrupt", json!({}));
        let _ = self.commands.send(Command::Close);
        if let Some(worker) = self.worker.lock().unwrap().take() {
            let _ = worker.join();
        }
        if let Some(agent) = &self.shared.agent {
            agent.close();
        }
        let _reviewed = self.shared.review(false);
        let indexed = !self.shared.enabled
            || self
                .shared
                .lore
                .lock()
                .unwrap()
                .index_transcript(&self.shared.cwd, &self.shared.session)
                .is_ok();
        indexed && !self.shared.failed.load(Ordering::Acquire)
    }
}
impl Host for ClaudeHost {
    fn has_active_work(&self) -> bool {
        self.shared.active.load(Ordering::Acquire)
    }
    fn can_set_model(&self) -> bool {
        true
    }
    fn model_change_requires_idle(&self) -> bool {
        true
    }
    fn can_set_permission_mode(&self) -> bool {
        true
    }
    fn initial_model(&self) -> Option<String> {
        self.shared.selection.lock().unwrap().0.clone()
    }
    fn initial_effort(&self) -> Option<String> {
        self.shared.selection.lock().unwrap().1.clone()
    }
    fn initial_permission_mode(&self) -> String {
        self.shared.selection.lock().unwrap().2.clone()
    }
    fn lore_enabled(&self) -> Option<bool> {
        Some(self.shared.enabled)
    }
    fn lore_status(&self) -> Option<Value> {
        self.shared.agent.as_ref()?.status()
    }
    fn lore_scrub_status(&self) -> Option<&'static str> {
        Some(if self.shared.failed.load(Ordering::Acquire) {
            "unavailable"
        } else {
            "ready"
        })
    }
    fn account_snapshot(&self) -> Option<Value> {
        self.shared.account.lock().unwrap().clone()
    }
    fn billing_snapshot(&self) -> Option<Value> {
        self.shared.billing.lock().unwrap().clone()
    }
    fn public_prompt(&self, text: &str) -> Result<String, String> {
        self.shared.scrub(text)
    }
    fn transcript_snapshot(&self) -> io::Result<Option<(PathBuf, u64)>> {
        self.shared.store.transcript_snapshot()
    }
    fn peer_tools_ready(&self) -> bool {
        self.shared.peer_allowed && self.shared.peer.lock().unwrap().is_some()
    }
    fn set_session_tool_handler(&self, handler: PeerToolHandler) -> bool {
        if self.shared.active.load(Ordering::Acquire) {
            return false;
        }
        if let Ok(mut slot) = self.shared.session_tools.lock() {
            if slot.is_none() {
                *slot = Some(handler);
                return true;
            }
        }
        false
    }
    fn set_peer_tool_handler(&self, handler: PeerToolHandler) -> bool {
        if !self.shared.peer_allowed || self.has_active_work() {
            return false;
        }
        let mut peer = self.shared.peer.lock().unwrap();
        if peer.is_some() {
            return false;
        }
        *peer = Some(handler);
        true
    }
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
        let (tx, rx) = mpsc::sync_channel(256);
        let (ack, answer) = mpsc::channel();
        {
            let _guard = self.admission.lock().unwrap();
            if self.shared.closing.load(Ordering::Acquire)
                || self.shared.failed.load(Ordering::Acquire)
            {
                emit(done("Claude session unavailable"));
                return;
            }
            if self.shared.active.swap(true, Ordering::AcqRel) {
                emit(done("Claude turn already running"));
                return;
            }
            self.shared.cancelled.store(false, Ordering::Release);
        }
        if text.trim_start().starts_with("/compact")
            && (text.trim() != "/compact" || !self.shared.review(true))
        {
            self.shared.active.store(false, Ordering::Release);
            emit(done(
                "LORE review failed or is disabled; compaction refused",
            ));
            return;
        }
        {
            let _guard = self.admission.lock().unwrap();
            if self.shared.cancelled.load(Ordering::Acquire)
                || self.shared.closing.load(Ordering::Acquire)
            {
                self.shared.active.store(false, Ordering::Release);
                emit(done("Claude turn interrupted"));
                return;
            }
            if self
                .commands
                .send(Command::Prompt(text.into(), tx, ack))
                .is_err()
            {
                self.shared.active.store(false, Ordering::Release);
                emit(done("Claude CLI closed"));
                return;
            }
        }
        if !matches!(answer.recv_timeout(RPC_TIMEOUT), Ok(Ok(_))) {
            emit(done("Claude refused prompt"));
            return;
        }
        while let Ok(event) = rx.recv() {
            let terminal = matches!(event["type"].as_str(), Some("turn_done" | "turn_refused"));
            emit(event);
            if terminal {
                return;
            }
        }
        emit(done("Claude CLI event stream closed"));
    }
    fn call(&self, method: &str, params: &Value) -> Result<Value, String> {
        match method {
            "set_model" | "set_effort" | "set_permission_mode" => {
                let _guard = self.admission.lock().unwrap();
                if (method != "set_permission_mode" && self.has_active_work())
                    || self.shared.closing.load(Ordering::Acquire)
                {
                    return Err("Claude setting changes require an idle session".into());
                }
                if method == "set_model"
                    && !params["model"].is_null()
                    && params["model"].as_str().is_none_or(|s| {
                        s.is_empty() || s.len() > 128 || s.chars().any(char::is_control)
                    })
                {
                    return Err("Invalid Claude model".into());
                }
                if method == "set_effort"
                    && !matches!(
                        params["effort"].as_str(),
                        Some("low" | "medium" | "high" | "xhigh" | "max")
                    )
                {
                    return Err("Unsupported Claude effort".into());
                }
                if method == "set_permission_mode"
                    && !matches!(
                        params["mode"].as_str(),
                        Some("default" | "acceptEdits" | "plan" | "auto" | "dontAsk")
                    )
                {
                    return Err("Unavailable Claude permission mode".into());
                }
                self.rpc(method, params.clone())
            }
            "interrupt" => {
                self.shared.cancelled.store(true, Ordering::Release);
                self.rpc(method, params.clone())
            }
            "list_models" | "context_detail" | "answer_needs_input" => {
                self.rpc(method, params.clone())
            }
            "stop" => {
                self.shutdown();
                Ok(json!({}))
            }
            _ => Err("Unsupported Claude operation".into()),
        }
    }
}
struct Operation {
    method: String,
    params: Value,
    reply: Reply,
    deadline: Instant,
    verify: bool,
}
enum PendingInput {
    Tool(Value),
    Mcp(Value),
}
fn send_event(sink: &Option<SyncSender<Value>>, value: Value) -> bool {
    sink.as_ref().is_some_and(|s| s.try_send(value).is_ok())
}
fn done(reason: &str) -> Value {
    json!({"type":"turn_done","data":{"error":reason,"is_error":true}})
}
fn provider_error(frame: &Value, shared: &Shared) -> Option<String> {
    let api_error = frame["isApiErrorMessage"] == true;
    let error_frame = frame["type"] == "error";
    let failed_result = frame["type"] == "result" && frame["is_error"] == true;
    if !api_error && !error_frame && !failed_result && frame["error"].is_null() {
        return None;
    }
    let mut parts = Vec::new();
    let mut api_text = String::new();
    if let Some(text) = frame["error"].as_str() {
        parts.push(text);
    }
    if frame["error"].is_object() {
        for key in ["type", "code", "message"] {
            if let Some(text) = frame["error"][key].as_str() {
                parts.push(text);
            }
        }
    }
    if api_error {
        if let Some(blocks) = frame["message"]["content"].as_array() {
            if blocks.len() > 32 {
                return Some(
                    "Claude provider error; diagnostic exceeded the safe display bound".into(),
                );
            }
            for block in blocks {
                if block["type"] == "text" {
                    if let Some(text) = block["text"].as_str() {
                        if api_text.len().saturating_add(text.len()) > 16 * 1024 {
                            return Some(
                                "Claude provider error; diagnostic exceeded the safe display bound"
                                    .into(),
                            );
                        }
                        // Plain text blocks are one diagnostic channel; adding
                        // whitespace here could hide a split key from LORE.
                        api_text.push_str(text);
                    }
                }
            }
            parts.push(&api_text);
        }
    }
    if error_frame {
        if let Some(text) = frame["message"].as_str() {
            parts.push(text);
        }
    }
    if failed_result {
        if let Some(errors) = frame["errors"].as_array() {
            if errors.len() > 8 {
                return Some(
                    "Claude provider error; diagnostic exceeded the safe display bound".into(),
                );
            }
            for error in errors {
                if let Some(text) = error.as_str().or_else(|| error["message"].as_str()) {
                    parts.push(text);
                }
            }
        }
        if let Some(text) = frame["result"].as_str() {
            parts.push(text);
        }
    }
    parts.retain(|text| !text.is_empty());
    if parts.is_empty() {
        return None;
    }
    // Refuse oversized raw diagnostics before truncation: cutting a credential
    // first could make its prefix invisible to the canonical matcher.
    if parts.iter().map(|s| s.len()).sum::<usize>() > 16 * 1024 {
        return Some("Claude provider error; diagnostic exceeded the safe display bound".into());
    }
    let raw = parts.join(" ");
    let lower = raw.to_ascii_lowercase();
    let auth = frame["error"] == "authentication_failed"
        || frame["error"]["type"] == "authentication_error"
        || [
            "failed to authenticate",
            "oauth session expired",
            "authentication failed",
            "invalid api key",
            "not logged in",
        ]
        .iter()
        .any(|s| lower.contains(s));
    let clean = match shared.scrub(&raw) {
        Ok(clean) => clean,
        Err(_) => return Some("Claude provider error; canonical diagnostic scrub failed".into()),
    };
    let mut clean = clean
        .chars()
        .filter(|c| !c.is_control() || *c == '\n')
        .collect::<String>();
    let mut end = clean.len().min(2048);
    while !clean.is_char_boundary(end) {
        end -= 1;
    }
    if end < clean.len() {
        clean.truncate(end);
        clean.push('…');
    }
    Some(if auth {
        format!("Claude authentication expired or was rejected. Open /setup and log in to Claude, or run `claude auth login`, then retry. {clean}")
    } else {
        format!("Claude provider error: {clean}")
    })
}
fn resolve(cli: &mut Cli, id: &str, request: &Value, answer: &Value) -> Result<(), Error> {
    let allowed = answer["decision"] == "allow";
    let response = if request["tool_name"] == "AskUserQuestion"
        && !answer["declined"].as_bool().unwrap_or(false)
        && answer["answers"].is_object()
    {
        let mut input = request["input"].clone();
        input["answers"] = answer["answers"].clone();
        json!({"behavior":"allow","updatedInput":input,"toolUseID":request["tool_use_id"]})
    } else if request["tool_name"] != "AskUserQuestion" && allowed {
        json!({"behavior":"allow","updatedInput":request["input"],"toolUseID":request["tool_use_id"]})
    } else {
        json!({"behavior":"deny","message":"User declined this tool request","interrupt":false,"toolUseID":request["tool_use_id"]})
    };
    cli.respond(id, Ok(response))
}
/// Block totals are summed across messages; opaque signatures are never content.
#[derive(Default)]
struct ReasoningProgress {
    tokens: u64,
    messages: HashMap<Option<String>, u64>,
    blocks: HashMap<(Option<String>, u64, u64), u64>,
}
impl ReasoningProgress {
    fn event(&mut self, event: &Value, parent: Option<&str>) -> Option<Value> {
        let parent = parent.map(str::to_owned);
        if event["type"] == "message_start" {
            if self.messages.len() < 32 || self.messages.contains_key(&parent) {
                let message = self.messages.entry(parent).or_default();
                *message = message.saturating_add(1);
            }
            return None;
        }
        let delta = &event["delta"];
        if delta["type"] != "thinking_delta" {
            return None;
        }
        let tokens = delta["estimated_tokens"].as_u64()?;
        let index = match event.get("index") {
            Some(value) => value.as_u64()?,
            None => 0,
        };
        if index > 1024 || tokens == 0 || tokens > 1_000_000 {
            return None;
        }
        let key = (
            parent.clone(),
            *self.messages.get(&parent).unwrap_or(&0),
            index,
        );
        let previous = *self.blocks.get(&key).unwrap_or(&0);
        if tokens <= previous || (!self.blocks.contains_key(&key) && self.blocks.len() >= 128) {
            return None;
        }
        let total = self
            .tokens
            .checked_add(tokens - previous)
            .filter(|value| *value <= 1_000_000)?;
        self.blocks.insert(key, tokens);
        self.tokens = total;
        let mut data = json!({"approx_tokens":total,"count_is_estimate":true});
        if let Some(parent) = parent {
            data["parent_id"] = json!(parent);
        }
        Some(json!({"type":"reasoning_progress","data":data}))
    }
}
struct StreamOutput {
    kind: String,
    parent: Option<String>,
    scrubber: StreamScrubber,
}
/// Public durable text is framed across assistant blocks and records, while
/// tool structure stays in its original record order. CLI state is untouched.
#[derive(Default)]
struct DurableTurn {
    records: Vec<Value>,
    bytes: usize,
    channels: Vec<(Option<String>, StreamScrubber)>,
}
struct DurableText {
    channel: usize,
    segment: usize,
    record: usize,
    block: usize,
    text: String,
}
impl DurableTurn {
    fn stage(&mut self, record: Value, shared: &Shared) -> Result<(), String> {
        let bytes = serde_json::to_vec(&record)
            .map_err(|_| "Invalid Claude durable record")?
            .len();
        if bytes > 1024 * 1024 {
            return Err("Claude durable record exceeded bound".into());
        }
        if self.bytes.saturating_add(bytes) > 256 * 1024 {
            self.flush(shared, false)?;
        }
        self.bytes += bytes;
        self.records.push(record);
        if self.bytes > 256 * 1024 {
            self.flush(shared, false)?;
        }
        Ok(())
    }
    fn flush(&mut self, shared: &Shared, terminal: bool) -> Result<(), String> {
        let mut records = std::mem::take(&mut self.records);
        self.bytes = 0;
        let mut outputs: Vec<DurableText> = Vec::new();
        let mut prefixes = Vec::new();
        let mut segment = 0;
        for (ri, record) in records.iter_mut().enumerate() {
            let assistant = record["type"] == "assistant";
            let has_text = assistant
                && record["message"]["content"]
                    .as_array()
                    .is_some_and(|blocks| blocks.iter().any(|b| b["type"] == "text"));
            let parent = record["parent_tool_use_id"].as_str().map(str::to_owned);
            let ci = if has_text {
                Some(
                    match self.channels.iter().position(|(id, _)| *id == parent) {
                        Some(ci) => ci,
                        None if self.channels.len() < 32 => {
                            self.channels.push((parent, StreamScrubber::default()));
                            self.channels.len() - 1
                        }
                        None => return Err("Claude durable channel bound exceeded".into()),
                    },
                )
            } else {
                None
            };
            let Some(blocks) = record["message"]["content"].as_array_mut() else {
                continue;
            };
            for (bi, block) in blocks.iter_mut().enumerate() {
                if matches!(block["type"].as_str(), Some("tool_use" | "tool_result")) {
                    self.separator(shared, &mut outputs, &mut prefixes)?;
                    segment += 1;
                    continue;
                }
                if !assistant || block["type"] != "text" {
                    continue;
                }
                let ci = ci.ok_or("Claude durable channel missing")?;
                let text = block["text"]
                    .as_str()
                    .ok_or("Invalid Claude durable text")?;
                let mut clean = String::new();
                let mut tail = text;
                while !tail.is_empty() {
                    let mut end = tail.len().min(65536);
                    while !tail.is_char_boundary(end) {
                        end -= 1;
                    }
                    clean.push_str(
                        &self.channels[ci]
                            .1
                            .push(&tail[..end], |s| shared.scrub(s).map_err(io::Error::other))
                            .map_err(|_| "Claude durable text framing refused")?,
                    );
                    tail = &tail[end..];
                }
                block["text"] = json!("");
                // Plain provider blocks have no visible separator. Combine only
                // inside one contiguous segment; real tools start a new segment.
                if let Some(output) = outputs
                    .iter_mut()
                    .find(|o| o.channel == ci && o.segment == segment)
                {
                    output.text.push_str(&clean);
                } else {
                    outputs.push(DurableText {
                        channel: ci,
                        segment,
                        record: ri,
                        block: bi,
                        text: clean,
                    });
                }
            }
        }
        if terminal {
            for (ci, (parent, stream)) in self.channels.iter_mut().enumerate() {
                let clean = stream
                    .finish(|s| shared.scrub(s).map_err(io::Error::other))
                    .map_err(|_| "Claude durable text boundary refused")?;
                if let Some(output) = outputs.iter_mut().rev().find(|o| o.channel == ci) {
                    output.text.push_str(&clean);
                } else if !clean.is_empty() {
                    let ri = records.len();
                    records.push(json!({"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":""}]},"parent_tool_use_id":parent,"sessionId":shared.session,"cwd":shared.cwd,"timestamp":crate::iso_now()}));
                    outputs.push(DurableText {
                        channel: ci,
                        segment,
                        record: ri,
                        block: 0,
                        text: clean,
                    });
                }
            }
        }
        for output in outputs {
            records[output.record]["message"]["content"][output.block]["text"] = json!(output.text);
        }
        for record in prefixes.into_iter().chain(records) {
            shared.persist(record)?;
        }
        if terminal {
            self.channels.clear();
        }
        Ok(())
    }
    fn separator(
        &mut self,
        shared: &Shared,
        outputs: &mut [DurableText],
        prefixes: &mut Vec<Value>,
    ) -> Result<(), String> {
        for (ci, (parent, stream)) in self.channels.iter_mut().enumerate() {
            let mut clean = stream
                .push("\n", |s| shared.scrub(s).map_err(io::Error::other))
                .map_err(|_| "Claude durable tool separator refused")?;
            clean.push_str(
                &stream
                    .finish(|s| shared.scrub(s).map_err(io::Error::other))
                    .map_err(|_| "Claude durable tool boundary refused")?,
            );
            if let Some(output) = outputs.iter_mut().rev().find(|o| o.channel == ci) {
                output.text.push_str(&clean);
            } else if !clean.trim().is_empty() {
                // This carry predates the current staged batch, so retain its
                // position before that batch rather than moving it after tools.
                prefixes.push(json!({"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":clean}]},"parent_tool_use_id":parent,"sessionId":shared.session,"cwd":shared.cwd,"timestamp":crate::iso_now()}));
            }
        }
        Ok(())
    }
}
impl StreamOutput {
    fn event(&self, text: String) -> Value {
        let mut data = json!({"text":text});
        if let Some(parent) = &self.parent {
            data["parent_id"] = json!(parent);
        }
        json!({"type":self.kind,"data":data})
    }
}
fn flush_streams(
    streams: &mut Vec<StreamOutput>,
    shared: &Shared,
    events: &Option<SyncSender<Value>>,
    visible_separator: bool,
) -> bool {
    let mut ok = true;
    let mut carried = std::mem::take(streams);
    for stream in &mut carried {
        let clean = (|| {
            let mut text = if visible_separator {
                stream
                    .scrubber
                    .push("\n", |s| shared.scrub(s).map_err(io::Error::other))?
            } else {
                String::new()
            };
            text.push_str(
                &stream
                    .scrubber
                    .finish(|s| shared.scrub(s).map_err(io::Error::other))?,
            );
            Ok::<_, io::Error>(text)
        })();
        match clean {
            Ok(text) if text.is_empty() => {}
            Ok(text) => {
                ok &= send_event(events, stream.event(text));
            }
            Err(_) => {
                ok = false;
            }
        }
    }
    // Visible tool separators delimit lexical tokens but remain whitespace for
    // canonical label matching. Keep recent label context across tool output.
    if visible_separator {
        *streams = carried;
    }
    ok
}
fn broker(mut cli: Cli, commands: Receiver<Command>, shared: Arc<Shared>) {
    let mut streams: Vec<StreamOutput> = Vec::new();
    let mut reasoning_progress = ReasoningProgress::default();
    let mut durable = DurableTurn::default();
    let mut operations: HashMap<String, Operation> = HashMap::new();
    let mut inputs: HashMap<String, PendingInput> = HashMap::new();
    let mut events = None;
    let mut overflow = false;
    let mut provider_failure: Option<String> = None;
    let mut stopping = false;
    let mut turn_deadline = None;
    let mut compact_permit = false;
    let mut terminal_context: Option<(String, Value, Instant)> = None;
    let (async_tx, async_rx) = mpsc::channel::<(String, Value)>();
    loop {
        while let Ok(command) = commands.try_recv() {
            match command {
                Command::Close => {
                    stopping = true;
                    let _ = cli.control(json!({"subtype":"interrupt"}));
                    turn_deadline = Some(Instant::now() + Duration::from_secs(3));
                    if events.is_none() {
                        return;
                    }
                }
                Command::Prompt(text, sink, reply) => {
                    streams.clear();
                    reasoning_progress = ReasoningProgress::default();
                    durable = DurableTurn::default();
                    events = Some(sink);
                    overflow = false;
                    provider_failure = None;
                    compact_permit = text.trim() == "/compact";
                    let ready = !shared.cancelled.load(Ordering::Acquire);
                    let admitted = (|| {
                        if !ready {
                            return Err("Claude prompt cancelled before admission".into());
                        }
                        shared.persist(json!({"type":"user","message":{"role":"user","content":text},"cwd":shared.cwd,"sessionId":shared.session,"timestamp":crate::iso_now()}))?;
                        shared.checkpoint(true)?;
                        let public_prompt = shared.scrub(&text)?;
                        cli.prompt(&text, &shared.session)
                            .map_err(|_| "Claude prompt write failed".to_owned())?;
                        Ok::<_, String>(public_prompt)
                    })();
                    if let Err(reason) = admitted {
                        let _ = reply.send(Err(reason.clone()));
                        send_event(&events, done(&reason));
                        events = None;
                        shared.active.store(false, Ordering::Release);
                    } else {
                        send_event(
                            &events,
                            json!({"type":"turn_started","data":{"prompt":admitted.unwrap()}}),
                        );
                        let _ = reply.send(Ok(json!({})));
                    }
                }
                Command::Rpc(method, params, reply) => {
                    if method == "answer_needs_input" {
                        let id = params["id"].as_str().unwrap_or("");
                        let mut applied = false;
                        if let Some(request) = inputs.remove(id) {
                            match request {
                                PendingInput::Tool(request) => {
                                    applied =
                                        resolve(&mut cli, id, &request, &params["answer"]).is_ok()
                                }
                                PendingInput::Mcp(request) => {
                                    applied = true;
                                    let allowed = params["answer"]["decision"] == "allow";
                                    let name = request["message"]["params"]["name"]
                                        .as_str()
                                        .unwrap_or("")
                                        .to_owned();
                                    let args = request["message"]["params"]["arguments"].clone();
                                    let rid = id.to_owned();
                                    let owned = shared.clone();
                                    let tx = async_tx.clone();
                                    thread::spawn(move || {
                                        let result = if allowed {
                                            owned.tool(&name, &args)
                                        } else {
                                            Err("Tool permission denied".into())
                                        };
                                        let response = match result {
                                            Ok(v) => match owned.scrub(&v.to_string()) {
                                                Ok(text) => {
                                                    json!({"content":[{"type":"text","text":text}]})
                                                }
                                                Err(_) => {
                                                    json!({"isError":true,"content":[{"type":"text","text":"Canonical tool scrub failed"}]})
                                                }
                                            },
                                            Err(_) => {
                                                json!({"isError":true,"content":[{"type":"text","text":"Tool request refused or failed; no success was verified"}]})
                                            }
                                        };
                                        let _=tx.send((rid,json!({"mcp_response":{"jsonrpc":"2.0","id":request["message"]["id"],"result":response}})));
                                    });
                                }
                            }
                            send_event(
                                &events,
                                json!({"type":"needs_input_resolved","data":{"id":id}}),
                            );
                        }
                        let _ = reply.send(Ok(json!({"applied":applied})));
                        continue;
                    }
                    let request = match method.as_str() {
                        "set_model" => json!({"subtype":"set_model","model":params["model"]}),
                        "set_effort" => {
                            json!({"subtype":"apply_flag_settings","settings":{"effortLevel":params["effort"]}})
                        }
                        "set_permission_mode" => {
                            json!({"subtype":"set_permission_mode","mode":if params["mode"]=="default"{json!("manual")}else{params["mode"].clone()}})
                        }
                        "list_models" => init_request(),
                        "context_detail" => json!({"subtype":"get_context_usage","detail":"full"}),
                        "interrupt" => {
                            turn_deadline = events
                                .as_ref()
                                .map(|_| Instant::now() + Duration::from_secs(5));
                            json!({"subtype":"interrupt"})
                        }
                        _ => {
                            let _ = reply.send(Err("Unsupported Claude control".into()));
                            continue;
                        }
                    };
                    match cli.control(request) {
                        Ok(id) => {
                            operations.insert(
                                id,
                                Operation {
                                    method,
                                    params,
                                    reply,
                                    deadline: Instant::now() + RPC_TIMEOUT,
                                    verify: false,
                                },
                            );
                        }
                        Err(_) => {
                            let _ = reply.send(Err("Claude control write failed".into()));
                            return;
                        }
                    }
                }
            }
        }
        while let Ok((id, value)) = async_rx.try_recv() {
            if cli.respond(&id, Ok(value)).is_err() {
                return;
            }
        }
        let now = Instant::now();
        if terminal_context
            .as_ref()
            .is_some_and(|(_, _, deadline)| now >= *deadline)
        {
            let (_, data, _) = terminal_context.take().unwrap();
            finish_terminal(&mut events, &shared, data);
            turn_deadline = None;
            compact_permit = false;
            if stopping {
                return;
            }
        }
        let expired: Vec<_> = operations
            .iter()
            .filter(|(_, o)| o.deadline <= now)
            .map(|(id, _)| id.clone())
            .collect();
        for id in expired {
            if let Some(op) = operations.remove(&id) {
                let _ = op.reply.send(Err(
                    "Claude control timed out; effective state is unknown".into()
                ));
                shared.failed.store(true, Ordering::Release);
            }
        }
        if turn_deadline.is_some_and(|deadline| now >= deadline) {
            cli.terminate();
            send_event(
                &events,
                done(
                    provider_failure
                        .as_deref()
                        .unwrap_or("Claude cancellation deadline expired; provider terminated"),
                ),
            );
            shared.active.store(false, Ordering::Release);
            return;
        }
        let frame = match cli.recv(POLL) {
            Ok(v) => v,
            Err(Error::Timeout) => continue,
            Err(_) => {
                send_event(
                    &events,
                    done(
                        provider_failure
                            .as_deref()
                            .unwrap_or("Claude CLI stream closed"),
                    ),
                );
                shared.active.store(false, Ordering::Release);
                return;
            }
        };
        if let Some(session) = frame["session_id"].as_str() {
            if session != shared.session {
                send_event(
                    &events,
                    done("Claude provider session identity changed; turn terminated"),
                );
                return;
            }
        }
        if shared.resume_identity_pending.load(Ordering::Acquire) {
            if frame["type"] == "system"
                && frame["subtype"] == "init"
                && frame["session_id"] == shared.session
            {
                shared
                    .resume_identity_pending
                    .store(false, Ordering::Release);
            } else if !matches!(
                frame["type"].as_str(),
                Some("control_request" | "control_response" | "rate_limit_event")
            ) {
                shared.failed.store(true, Ordering::Release);
                shared.active.store(false, Ordering::Release);
                send_event(
                    &events,
                    done("Resumed Claude provider identity was not confirmed; output withheld"),
                );
                return;
            }
        }
        if let Some(error) = provider_error(&frame, &shared) {
            if provider_failure.is_none() || error.contains("Open /setup") {
                provider_failure = Some(error);
            }
        }
        match frame["type"].as_str() {
            Some("error") => {
                let _ = cli.control(json!({"subtype":"interrupt"}));
                turn_deadline = Some(Instant::now() + Duration::from_secs(5));
            }
            Some("control_response") => {
                let id = frame["response"]["request_id"].as_str().unwrap_or("");
                if terminal_context
                    .as_ref()
                    .is_some_and(|(request, _, _)| request == id)
                {
                    let (_, mut data, _) = terminal_context.take().unwrap();
                    if frame["response"]["subtype"] == "success" {
                        let context = &frame["response"]["response"];
                        data["ctx_percentage"] = context["percentage"]
                            .as_f64()
                            .filter(|v| v.is_finite() && (0.0..=100.0).contains(v))
                            .map_or(Value::Null, |v| json!(v));
                        data["ctx_tokens"] = context["totalTokens"]
                            .as_u64()
                            .map_or(Value::Null, |v| json!(v));
                        data["ctx_max_tokens"] = context["maxTokens"]
                            .as_u64()
                            .filter(|v| *v > 0)
                            .map_or(Value::Null, |v| json!(v));
                    }
                    finish_terminal(&mut events, &shared, data);
                    turn_deadline = None;
                    compact_permit = false;
                    if stopping {
                        return;
                    }
                    continue;
                }
                let Some(mut op) = operations.remove(id) else {
                    continue;
                };
                if frame["response"]["subtype"] != "success" {
                    let _ = op
                        .reply
                        .send(Err("Claude refused setting or control request".into()));
                    continue;
                }
                let value = &frame["response"]["response"];
                if matches!(op.method.as_str(), "set_model" | "set_effort") && !op.verify {
                    op.verify = true;
                    match cli.control(json!({"subtype":"get_settings"})) {
                        Ok(id) => {
                            operations.insert(id, op);
                        }
                        Err(_) => {
                            let _ = op
                                .reply
                                .send(Err("Claude effective settings verification failed".into()));
                        }
                    }
                    continue;
                }
                let result = match op.method.as_str() {
                    "list_models" => {
                        if let Some(account) = display_account(&value["account"]) {
                            *shared.account.lock().unwrap() = Some(account.clone());
                            set_billing_account(&shared, &account);
                        }
                        *shared.catalog.lock().unwrap() = value.clone();
                        catalog(value)
                    }
                    "set_model" | "set_effort" => match settings(value) {
                        Ok((model, effort)) => {
                            let applied = if op.method == "set_effort" {
                                effort.as_deref() == op.params["effort"].as_str()
                            } else {
                                model_applied(
                                    &shared.catalog.lock().unwrap(),
                                    op.params["model"].as_str().unwrap_or("default"),
                                    model.as_deref(),
                                )
                            };
                            let mut selected = shared.selection.lock().unwrap();
                            selected.0 = model.clone();
                            selected.1 = effort.clone();
                            drop(selected);
                            if !applied {
                                Err("Claude provider did not apply the requested setting".into())
                            } else {
                                shared.checkpoint(!shared.store.transcript_path().exists()).map(|_|if op.method=="set_model"{json!({"model":model,"effort":effort,"verified":true})}else{json!({"effort":effort,"model":model,"verified":true,"verification_pending":false})})
                            }
                        }
                        Err(e) => Err(e),
                    },
                    "set_permission_mode" => {
                        let mode = op.params["mode"].as_str().unwrap_or("default");
                        let wire = if mode == "default" { "manual" } else { mode };
                        if value["mode"] != wire
                            && !(wire == "manual" && value["mode"] == "default")
                        {
                            Err("Claude permission mode was not verified".into())
                        } else {
                            shared.selection.lock().unwrap().2 = mode.into();
                            Ok(json!({"mode":mode}))
                        }
                    }
                    "interrupt" => Ok(json!({})),
                    "context_detail" => context_detail(value, &shared),
                    _ => Err("Unsupported Claude reply".into()),
                };
                let _ = op.reply.send(result);
            }
            Some("control_request") => {
                let id = frame["request_id"].as_str().unwrap_or("").to_owned();
                if id.is_empty() || id.len() > 128 || inputs.len() >= 32 {
                    let _ = cli.respond(&id, Err("invalid request"));
                    continue;
                }
                let request = &frame["request"];
                match request["subtype"].as_str() {
                    Some("can_use_tool") => {
                        if !request["input"].is_object() || !request["tool_name"].is_string() {
                            let _ = cli.respond(&id, Err("invalid tool request"));
                            continue;
                        }
                        let clean = match shared.clean(request) {
                            Ok(v) => v,
                            Err(_) => {
                                let _ =
                                    resolve(&mut cli, &id, request, &json!({"decision":"deny"}));
                                continue;
                            }
                        };
                        let data = if request["tool_name"] == "AskUserQuestion" {
                            json!({"id":id,"kind":"ask_user","tool_name":"AskUserQuestion","questions":clean["input"]["questions"]})
                        } else {
                            json!({"id":id,"kind":"permission","tool_name":clean["tool_name"],"title":clean["title"],"display_name":clean["display_name"],"description":clean["description"],"input_summary":clean["input"].to_string(),"require_full_review":true})
                        };
                        if send_event(&events, json!({"type":"needs_input","data":data})) {
                            inputs.insert(id, PendingInput::Tool(request.clone()));
                        } else {
                            let _ = resolve(&mut cli, &id, request, &json!({"decision":"deny"}));
                        }
                    }
                    Some("mcp_message") => {
                        if request["server_name"] != "doxa" {
                            let _ = cli.respond(&id, Err("unknown server"));
                            continue;
                        }
                        if let Some(response) = mcp_reply(request, &shared) {
                            let _ = cli.respond(&id, Ok(response));
                        } else if request["message"]["method"] == "tools/call" {
                            let clean = match shared.clean(request) {
                                Ok(v) => v,
                                Err(_) => {
                                    let _ = cli.respond(&id, Err("scrub failed"));
                                    continue;
                                }
                            };
                            let name = clean["message"]["params"]["name"].as_str().unwrap_or("");
                            if !shared.tools().iter().any(|r| r["name"] == name) {
                                let _ = cli.respond(&id, Err("unavailable tool"));
                                continue;
                            }
                            let data = json!({"id":id,"kind":"permission","tool_name":format!("mcp__doxa__{name}"),"title":"Approve this canonical DOXA tool once?","input_summary":clean["message"]["params"]["arguments"].to_string(),"require_full_review":true});
                            if send_event(&events, json!({"type":"needs_input","data":data})) {
                                inputs.insert(id, PendingInput::Mcp(request.clone()));
                            } else {
                                let _ = cli.respond(&id, Err("no human decision"));
                            }
                        } else {
                            let _=cli.respond(&id,Ok(json!({"mcp_response":{"jsonrpc":"2.0","id":request["message"]["id"],"result":{}}})));
                        }
                    }
                    Some("hook_callback") => {
                        if request["callback_id"] == "doxa_pre_compact"
                            && durable.flush(&shared, false).is_err()
                        {
                            shared.failed.store(true, Ordering::Release);
                            let _ = cli.respond(&id, Ok(json!({"decision":"block","reason":"Canonical durable transcript framing failed"})));
                            continue;
                        }
                        if request["callback_id"] == "doxa_pre_compact" && compact_permit {
                            compact_permit = false;
                            let _ = cli.respond(&id, Ok(json!({})));
                        } else if request["callback_id"] == "doxa_pre_compact" {
                            let tx = async_tx.clone();
                            let owned = shared.clone();
                            thread::spawn(move || {
                                let value = if owned.review(true) {
                                    json!({})
                                } else {
                                    json!({"decision":"block","reason":"LORE review failed or is disabled; compaction refused"})
                                };
                                let _ = tx.send((id, value));
                            });
                        } else if request["callback_id"] == "doxa_prompt_context" {
                            let context = if shared.enabled {
                                shared
                                    .lore
                                    .lock()
                                    .unwrap()
                                    .snapshot(&shared.cwd, "all")
                                    .unwrap_or_default()
                            } else {
                                "[DOXA MEMORY OFF] This session has memory disabled. Do not use LORE memory tools.".into()
                            };
                            let value = json!({"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":context}});
                            let _ = cli.respond(&id, Ok(value));
                        } else {
                            let _ = cli.respond(&id, Err("unknown hook"));
                        }
                    }
                    _ => {
                        let _ = cli.respond(&id, Err("unsupported control"));
                    }
                }
            }
            Some("control_cancel_request") => {
                if let Some(id) = frame["request_id"].as_str() {
                    inputs.remove(id);
                    send_event(
                        &events,
                        json!({"type":"needs_input_resolved","data":{"id":id}}),
                    );
                }
            }
            Some("stream_event") => {
                let event = &frame["event"];
                // Provider content blocks have no guaranteed visible separator.
                // Keep channel carry across block stops and assistant summaries.
                let delta = &event["delta"];
                if let Some(progress) =
                    reasoning_progress.event(event, frame["parent_tool_use_id"].as_str())
                {
                    if !overflow && !send_event(&events, progress) {
                        overflow = true;
                        let _ = cli.control(json!({"subtype":"interrupt"}));
                    }
                }

                if let Some((kind, text)) = match delta["type"].as_str() {
                    Some("text_delta") => delta["text"].as_str().map(|s| ("text_delta", s)),
                    Some("thinking_delta") => {
                        delta["thinking"].as_str().map(|s| ("reasoning_delta", s))
                    }
                    _ => None,
                } {
                    let parent = frame["parent_tool_use_id"].as_str();
                    let position = streams
                        .iter()
                        .position(|s| s.kind == kind && s.parent.as_deref() == parent);
                    let position = match position {
                        Some(i) => i,
                        None if streams.len() < 32 => {
                            streams.push(StreamOutput {
                                kind: kind.into(),
                                parent: parent.map(str::to_owned),
                                scrubber: StreamScrubber::default(),
                            });
                            streams.len() - 1
                        }
                        None => {
                            shared.failed.store(true, Ordering::Release);
                            let _ = cli.control(json!({"subtype":"interrupt"}));
                            continue;
                        }
                    };
                    let stream = &mut streams[position];
                    let clean = stream
                        .scrubber
                        .push(text, |s| shared.scrub(s).map_err(io::Error::other));
                    let delivered = match clean {
                        Ok(text) if text.is_empty() => true,
                        Ok(text) => !overflow && send_event(&events, stream.event(text)),
                        Err(_) => {
                            shared.failed.store(true, Ordering::Release);
                            false
                        }
                    };
                    if !delivered {
                        overflow = true;
                        let _ = cli.control(json!({"subtype":"interrupt"}));
                        turn_deadline = Some(Instant::now() + Duration::from_secs(5));
                    }
                }
            }
            Some("assistant" | "user") => {
                let has_tools = frame["message"]["content"]
                    .as_array()
                    .is_some_and(|blocks| {
                        blocks
                            .iter()
                            .any(|b| matches!(b["type"].as_str(), Some("tool_use" | "tool_result")))
                    });
                if has_tools && !flush_streams(&mut streams, &shared, &events, true) {
                    shared.failed.store(true, Ordering::Release);
                    let _ = cli.control(json!({"subtype":"interrupt"}));
                    turn_deadline = Some(Instant::now() + Duration::from_secs(5));
                }
                let kind = frame["type"].as_str().unwrap();
                if frame["message"].is_object() {
                    let mut message = frame["message"].clone();
                    if let Some(blocks) = message["content"].as_array_mut() {
                        blocks.retain(|b| {
                            !matches!(b["type"].as_str(), Some("thinking" | "redacted_thinking"))
                        });
                    }
                    if durable.stage(json!({"type":kind,"message":message,"parent_tool_use_id":frame["parent_tool_use_id"],"sessionId":shared.session,"cwd":shared.cwd,"timestamp":crate::iso_now()}), &shared).is_err(){shared.failed.store(true, Ordering::Release); let _=cli.control(json!({"subtype":"interrupt"})); turn_deadline=Some(Instant::now()+Duration::from_secs(5));}
                    for block in frame["message"]["content"].as_array().into_iter().flatten() {
                        let event = match block["type"].as_str() {
                            Some("tool_use") => Some(
                                json!({"type":"tool_call","data":{"id":block["id"],"name":block["name"],"input":block["input"],"parent_id":frame["parent_tool_use_id"]}}),
                            ),
                            Some("tool_result") => Some(
                                json!({"type":"tool_result","data":{"id":block["tool_use_id"],"result_summary":block["content"],"is_error":block["is_error"]}}),
                            ),
                            _ => None,
                        };
                        if let Some(event) = event {
                            if let Ok(clean) = shared.clean(&event) {
                                send_event(&events, clean);
                            }
                        }
                    }
                }
            }
            Some("result") => {
                if durable.flush(&shared, true).is_err() {
                    shared.failed.store(true, Ordering::Release);
                }
                if !flush_streams(&mut streams, &shared, &events, false) {
                    shared.failed.store(true, Ordering::Release);
                }
                let error = frame["is_error"] == true
                    || provider_failure.is_some()
                    || overflow
                    || shared.failed.load(Ordering::Acquire);
                for (id, request) in inputs.drain() {
                    if let PendingInput::Tool(request) = request {
                        let _ = resolve(&mut cli, &id, &request, &json!({"decision":"deny"}));
                    }
                    send_event(
                        &events,
                        json!({"type":"needs_input_resolved","data":{"id":id}}),
                    );
                }
                if !error && shared.checkpoint(false).is_err() {
                    shared.failed.store(true, Ordering::Release);
                }
                let usage = &frame["usage"];
                let reason = if overflow {
                    Some("Claude event stream overflowed; turn cancelled")
                } else if error {
                    Some(
                        provider_failure
                            .as_deref()
                            .unwrap_or("Claude turn failed or was interrupted"),
                    )
                } else {
                    None
                };
                let data = json!({"ctx_percentage":null,"ctx_tokens":null,"ctx_max_tokens":null,"input_tokens":usage["input_tokens"],"output_tokens":usage["output_tokens"],"cache_read_input_tokens":usage["cache_read_input_tokens"],"cache_creation_input_tokens":usage["cache_creation_input_tokens"],"cost_usd":frame["total_cost_usd"],"is_error":error||shared.failed.load(Ordering::Acquire),"num_turns":frame["num_turns"],"error":reason});
                match cli.control(json!({"subtype":"get_context_usage","detail":"summary"})) {
                    Ok(id) => {
                        terminal_context =
                            Some((id, data, Instant::now() + Duration::from_secs(3)));
                    }
                    Err(_) => {
                        finish_terminal(&mut events, &shared, data);
                        turn_deadline = None;
                        compact_permit = false;
                        if stopping {
                            return;
                        }
                    }
                }
            }
            Some("rate_limit_event") => {
                let data = &frame["rate_limit_info"];
                for projected in rate_limits(data) {
                    send_event(&events, json!({"type":"rate_limit","data":projected}));
                    if let Some(billing) = update_quota(&shared, &projected) {
                        send_event(&events, json!({"type":"billing","data":billing}));
                    }
                }
            }
            Some("system") => {
                if frame["subtype"] == "compact_boundary" {
                    send_event(
                        &events,
                        json!({"type":"compaction","data":{"reason":"Claude context compacted after canonical LORE review"}}),
                    );
                }
            }
            _ => {}
        }
    }
}
fn display_account(value: &Value) -> Option<Value> {
    let mut result = serde_json::Map::new();
    for key in ["email", "organization", "subscriptionType", "apiProvider"] {
        if let Some(text) = value[key]
            .as_str()
            .filter(|s| !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control))
        {
            result.insert(key.into(), json!(text));
        }
    }
    (!result.is_empty()).then_some(Value::Object(result))
}
fn billing_from_account(account: &Value) -> Option<Value> {
    let tier = account["subscriptionType"].as_str()?;
    Some(json!({"mode":"subscription","type":tier,"quota":null}))
}

fn finish_terminal(events: &mut Option<SyncSender<Value>>, shared: &Shared, data: Value) {
    if let Some(sink) = events.take() {
        let _ = sink.send(json!({"type":"turn_done","data":data}));
    }
    shared.active.store(false, Ordering::Release);
}
fn context_detail(value: &Value, shared: &Shared) -> Result<Value, String> {
    let mut result = json!({"source":"Claude CLI get_context_usage"});
    for (wire, key) in [
        ("categories", "categories"),
        ("memoryFiles", "memory_files"),
        ("mcpTools", "mcp_tools"),
        ("agents", "agents"),
        ("totalTokens", "total_tokens"),
        ("maxTokens", "max_tokens"),
        ("rawMaxTokens", "raw_max_tokens"),
        ("autoCompactThreshold", "autocompact_threshold"),
        ("isAutoCompactEnabled", "autocompact_enabled"),
        ("model", "model"),
        ("percentage", "percentage"),
    ] {
        if let Some(field) = value.get(wire) {
            if !matches!(field,Value::Array(a)if a.len()>128) {
                result[key] = shared.clean(field)?;
            }
        }
    }
    Ok(result)
}
// Both legacy limiting-window fields and modern per-window snapshots use the
// same validation. Missing fields are partial updates; malformed present fields
// refuse only that window, leaving independent valid windows usable.
fn quota_window(window: &str, value: &Value, status: Option<&str>) -> Option<Value> {
    if !matches!(
        window,
        "five_hour" | "seven_day" | "seven_day_opus" | "seven_day_sonnet"
            | "seven_day_overage_included" | "overage"
    ) || !value.is_object() {
        return None;
    }
    let mut result = json!({"window":window});
    if let Some(status) = status {
        if !matches!(status, "allowed" | "allowed_warning" | "rejected") {
            return None;
        }
        result["status"] = json!(status);
    }
    if let Some(utilization) = value.get("utilization") {
        let utilization = utilization
            .as_f64()
            .filter(|v| v.is_finite() && (0.0..=1.0).contains(v))?;
        result["percent"] = json!((utilization * 100.0).round() as u64);
    }
    if let Some(reset) = value.get("resetsAt") {
        result["resets_at"] = json!(reset.as_u64().filter(|v| *v <= 253402300799)?);
    }
    (result.as_object()?.len() > 1).then_some(result)
}
fn rate_limits(value: &Value) -> Vec<Value> {
    let mut limits = Vec::new();
    if let (Some(window), Some(status)) =
        (value["rateLimitType"].as_str(), value["status"].as_str())
    {
        if let Some(limit) = quota_window(window, value, Some(status)) {
            limits.push(limit);
        }
    }
    if let Some(windows) = value["unifiedWindows"].as_object() {
        for window in ["five_hour", "seven_day", "seven_day_overage_included"] {
            if let Some(row) = windows.get(window) {
                if let Some(limit) = quota_window(window, row, None) {
                    limits.push(limit);
                }
            }
        }
    }
    limits
}
fn set_billing_account(shared: &Shared, account: &Value) {
    *shared.billing.lock().unwrap() = billing_from_account(account);
    let _ = render_quota(shared);
}
fn render_quota(shared: &Shared) -> Option<Value> {
    let limits = shared.quota_limits.lock().ok()?.clone();
    if limits.as_object()?.is_empty() {
        return shared.billing.lock().ok()?.clone();
    }
    let mut cached = shared.billing.lock().ok()?;
    let billing = cached.as_mut()?;
    if billing["mode"] != "subscription" {
        return None;
    }
    billing["quota_limits"] = limits;
    let mut text = Vec::new();
    for (key, label) in [
        ("five_hour", "5h"), ("seven_day", "week"), ("seven_day_opus", "opus"),
        ("seven_day_sonnet", "sonnet"), ("seven_day_overage_included", "included"), ("overage", "extra"),
    ] {
        if let Some(percent) = billing["quota_limits"][key]["percent"].as_u64() {
            text.push(format!("{label}:{percent}%"));
        }
    }
    billing["quota"] = if text.is_empty() { Value::Null } else { json!(text.join(" ")) };
    billing["quota_source"] = json!("claude_cli");
    billing["quota_stale"] = json!(false);
    Some(billing.clone())
}
fn update_quota(shared: &Shared, limit: &Value) -> Option<Value> {
    let window = limit["window"].as_str()?;
    {
        let mut limits = shared.quota_limits.lock().ok()?;
        if !limits[window].is_object() {
            limits[window] = json!({});
        }
        let row = limits[window].as_object_mut()?;
        if let Some(reset) = limit.get("resets_at") {
            if row.get("resets_at") != Some(reset) {
                // A new period cannot inherit the previous period's utilization
                // or limiting status. An accompanying report can replace them.
                if limit.get("percent").is_none() {
                    row.remove("percent");
                }
                if limit.get("status").is_none() {
                    row.remove("status");
                }
            }
        }
        for (key, value) in limit.as_object()? {
            if key != "window" {
                row.insert(key.clone(), value.clone());
            }
        }
        row.insert("source".into(), json!("claude_cli"));
        row.insert("stale".into(), json!(false));
    }
    render_quota(shared)
}

fn model_applied(catalog: &Value, requested: &str, effective: Option<&str>) -> bool {
    let expected = catalog["models"]
        .as_array()
        .and_then(|rows| rows.iter().find(|r| r["value"] == requested))
        .and_then(|r| r["resolvedModel"].as_str())
        .unwrap_or(requested);
    effective == Some(expected)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::PermissionsExt};
    const SESSION: &str = "0b256c09-8d74-4865-9be0-4e6d24384551";
    fn fixture(body: &str) -> (tempfile::TempDir, Arc<ClaudeHost>) {
        fixture_with_startup(
            body, "pass",
            Some(json!({"mode":"subscription","type":"max","quota":null})),
        )
    }
    fn fixture_with_startup(
        body: &str, startup: &str, initial_billing: Option<Value>,
    ) -> (tempfile::TempDir, Arc<ClaudeHost>) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("claude");
        fs::write(&path,format!(r#"#!/usr/bin/env python3
import sys,json,os,time
model='claude-opus-5-5';effort='high'
def emit(row): print(json.dumps(row),flush=True)
for line in sys.stdin:
 row=json.loads(line)
 if row['type']=='control_request':
  request=row['request'];sub=request['subtype'];response={{}}
  if sub=='initialize': response={{'models':[{{'value':'opus','resolvedModel':'claude-opus-5-5','supportedEffortLevels':['low','high']}},{{'value':'sonnet','resolvedModel':'claude-sonnet-5','supportedEffortLevels':['low','high']}}]}}
  elif sub=='get_settings': response={{'applied':{{'model':model,'effort':effort}}}}
  elif sub=='apply_flag_settings': effort=request['settings']['effortLevel']
  elif sub=='set_model': model='claude-'+request['model']+'-5' if request['model']=='sonnet' else request['model']
  elif sub=='get_context_usage':response={{'totalTokens':1234,'maxTokens':1000000,'percentage':.1234}}
  elif sub=='set_permission_mode':response={{'mode':'default' if request['mode']=='manual' else request['mode']}}
  {startup}
  emit({{'type':'control_response','response':{{'subtype':'success','request_id':row['request_id'],'response':response}}}})
  if sub=='interrupt':emit({{'type':'result','session_id':'{SESSION}','is_error':True}})
 {body}
"#)).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        let store = TranscriptStore::new(dir.path(), "project", SESSION).unwrap();
        let shared = Arc::new(Shared {
            session: SESSION.into(),
            cwd: dir.path().to_string_lossy().into_owned(),
            lore: Mutex::new(LoreClient::open(Duration::from_secs(3)).unwrap()),
            store,
            enabled: false,
            failed: AtomicBool::new(false),
            active: AtomicBool::new(false),
            closing: AtomicBool::new(false),
            cancelled: AtomicBool::new(false),
            resume_identity_pending: AtomicBool::new(false),
            selection: Mutex::new((
                Some("claude-opus-5-5".into()),
                Some("high".into()),
                "default".into(),
            )),
            account: Mutex::new(None),
            billing: Mutex::new(initial_billing),
            quota_limits: Mutex::new(json!({})),
            catalog: Mutex::new(Value::Null),
            agent: None,
            peer: Mutex::new(None),
            session_tools: Mutex::new(None),
            peer_allowed: false,
            depth: 0,
            parent: None,
        });
        let mut cli = Cli::spawn(CliOptions {
            executable: &path,
            cwd: dir.path(),
            session_id: SESSION,
            resume: false,
            model: None,
            effort: None,
            permission_mode: "manual",
            config_dir: dir.path(),
            plugins: &[],
        })
        .unwrap();
        let initial = startup_control(
            &mut cli,
            init_request(),
            &shared,
            Instant::now() + Duration::from_secs(3),
        )
        .unwrap();
        *shared.catalog.lock().unwrap() = initial;
        let (tx, rx) = mpsc::channel();
        let worker_shared = shared.clone();
        let worker = thread::spawn(move || broker(cli, rx, worker_shared));
        let host = Arc::new(ClaudeHost {
            commands: tx,
            worker: Mutex::new(Some(worker)),
            shared,
            admission: Mutex::new(()),
        });
        (dir, host)
    }
    #[test]
    fn expired_cli_auth_reports_scrubbed_login_guidance() {
        let body = r#"if row['type']=='user':
  assert row['message']['content']=='private task'
  emit({'type':'assistant','session_id':'$SESSION','isApiErrorMessage':True,'error':'authentication_failed','message':{'role':'assistant','content':[{'type':'text','text':'Failed to authenticate: OAuth session expired and could not be refreshed. token=sk-abcdefghijklmnopqrstuvwxyz123456'}]}})
  emit({'type':'result','session_id':'$SESSION','is_error':True,'errors':['Claude turn failed']})
"#.replace("$SESSION", SESSION);
        let (_dir, host) = fixture(&body);
        let mut events = Vec::new();
        host.prompt("private task", &mut |event| events.push(event));
        let terminal = events.last().unwrap();
        assert_eq!(terminal["type"], "turn_done");
        assert_eq!(terminal["data"]["is_error"], true);
        let error = terminal["data"]["error"].as_str().unwrap();
        assert!(error.contains("OAuth session expired"), "{error}");
        assert!(error.contains("/setup"));
        assert!(error.contains("claude auth login"));
        assert!(error.contains("[REDACTED:"));
        assert!(!error.contains("abcdefghijkl"));
        assert!(!error.contains("private task"));
        assert!(host.shutdown());
    }
    #[test]
    fn provider_error_fields_are_bounded_scrubbed_and_failure_only() {
        let (_dir, host) = fixture("");
        let shared = &host.shared;
        assert!(provider_error(&json!({"type":"assistant","message":{"content":[{"type":"text","text":"Discuss OAuth session expired"}]}}), shared).is_none());
        let message = provider_error(&json!({"type":"result","is_error":true,"errors":["Service overloaded sk-abcdefghijklmnopqrstuvwxyz123456"],"prompt":"never echo this"}), shared).unwrap();
        assert!(message.contains("Service overloaded [REDACTED:api-key]"));
        assert!(!message.contains("never echo"));
        assert!(!message.contains("/setup"));
        let message = provider_error(&json!({"type":"assistant","isApiErrorMessage":true,"message":{"content":[{"type":"text","text":"API error sk-"},{"type":"text","text":"abcdefghijklmnopqrstuvwxyz123456"}]}}), shared).unwrap();
        assert!(message.contains("[REDACTED:api-key]"));
        assert!(!message.contains("abcdefgh"));
        let message = provider_error(&json!({"type":"error","error":{"type":"authentication_error","message":"Token was rejected"}}), shared).unwrap();
        assert!(message.contains("claude auth login"));
        let message = provider_error(
            &json!({"type":"result","is_error":true,"errors":["x".repeat(16*1024+1)]}),
            shared,
        )
        .unwrap();
        assert!(message.contains("safe display bound"));
        assert!(message.len() < 100);
        assert!(host.shutdown());
    }
    #[test]
    fn durable_prose_keeps_contiguous_blocks_and_real_tool_separators() {
        let body = r#"if row['type']=='user':
  emit({'type':'assistant','session_id':'$SESSION','message':{'role':'assistant','content':[{'type':'text','text':'I will '},{'type':'text','text':'read.'}]}})
  emit({'type':'assistant','session_id':'$SESSION','message':{'role':'assistant','content':[{'type':'tool_use','id':'exact-tool','name':'fixture','input':{}}]}})
  emit({'type':'user','session_id':'$SESSION','message':{'role':'user','content':[{'type':'tool_result','tool_use_id':'exact-tool','content':'exact result'}]}})
  for text in ['The result ', 'is correct.']:
   emit({'type':'assistant','session_id':'$SESSION','message':{'role':'assistant','content':[{'type':'text','text':text}]}})
  emit({'type':'result','session_id':'$SESSION','is_error':False})
"#.replace("$SESSION", SESSION);
        let (_dir, host) = fixture(&body);
        host.prompt("task", &mut |_| {});
        let bytes = fs::read(host.shared.store.transcript_path()).unwrap();
        let restored = doxa_tui::history::render(&doxa_tui::transport::TranscriptSnapshot {
            bytes,
            earlier_bytes_omitted: false,
        });
        assert!(restored.contains("I will read."), "{restored}");
        assert!(restored.contains("The result is correct."), "{restored}");
        assert!(!restored.contains("read.The"));
        let records = host.shared.store.read_records().unwrap();
        let before = records
            .iter()
            .position(|r| {
                r["message"]["content"][0]["text"]
                    .as_str()
                    .is_some_and(|t| t.starts_with("I will"))
            })
            .unwrap();
        let tool = records
            .iter()
            .position(|r| r["message"]["content"][0]["type"] == "tool_use")
            .unwrap();
        let result = records
            .iter()
            .position(|r| r["message"]["content"][0]["type"] == "tool_result")
            .unwrap();
        let after = records
            .iter()
            .position(|r| {
                r["message"]["content"][0]["text"]
                    .as_str()
                    .is_some_and(|t| t.starts_with("The result"))
            })
            .unwrap();
        assert!(before < tool && tool < result && result < after);
        assert!(host.shutdown());
    }
    #[test]
    fn durable_split_text_is_safe_on_actual_tui_replay() {
        let body = r#"if row['type']=='user':
  for chunks in [['safe ', 'sk-', 'abcdefghijklmnop', 'qrstuvwxyz123456 '], ['Bearer ', 'abcdefghijklmnop', 'qrstuvwxyz '], ["password='long ", 'secret ', "phrase' "], ['-----BEGIN PRIVATE KEY-----\n', 'fixturePrivateMaterial\n', '-----END PRIVATE KEY----- ']]:
   emit({'type':'assistant','session_id':'$SESSION','message':{'role':'assistant','content':[{'type':'text','text':chunk} for chunk in chunks[:2]]}})
   for chunk in chunks[2:]:
    emit({'type':'assistant','session_id':'$SESSION','message':{'role':'assistant','content':[{'type':'text','text':chunk}]}})
  emit({'type':'assistant','session_id':'$SESSION','message':{'role':'assistant','content':[{'type':'tool_use','id':'exact-tool','name':'fixture','input':{'path':'preserved'}}]}})
  emit({'type':'user','session_id':'$SESSION','message':{'role':'user','content':[{'type':'tool_result','tool_use_id':'exact-tool','content':'exact result','is_error':False}]}})
  emit({'type':'result','session_id':'$SESSION','is_error':False})
"#.replace("$SESSION", SESSION);
        let (_dir, host) = fixture(&body);
        let mut events = Vec::new();
        host.prompt("task", &mut |event| events.push(event));
        assert_eq!(events.last().unwrap()["data"]["is_error"], false);
        let bytes = fs::read(host.shared.store.transcript_path()).unwrap();
        let restored = doxa_tui::history::render(&doxa_tui::transport::TranscriptSnapshot {
            bytes: bytes.clone(),
            earlier_bytes_omitted: false,
        });
        for public in [String::from_utf8(bytes).unwrap(), restored] {
            for secret in ["abcdefgh", "long ", "secret ", "phrase'", "fixturePrivate"] {
                assert!(!public.contains(secret), "{public}");
            }
            for marker in ["api-key", "bearer", "value", "pem"] {
                assert!(public.contains(&format!("[REDACTED:{marker}]")), "{public}");
            }
        }
        let records = host.shared.store.read_records().unwrap();
        let tool = records
            .iter()
            .flat_map(|r| r["message"]["content"].as_array().into_iter().flatten())
            .find(|b| b["type"] == "tool_use")
            .unwrap();
        assert_eq!(tool["id"], "exact-tool");
        assert_eq!(tool["input"]["path"], "preserved");
        let result = records
            .iter()
            .flat_map(|r| r["message"]["content"].as_array().into_iter().flatten())
            .find(|b| b["type"] == "tool_result")
            .unwrap();
        assert_eq!(result["tool_use_id"], "exact-tool");
        assert_eq!(result["content"], "exact result");
        let saved = host.shared.store.read_thread().unwrap().unwrap();
        assert_eq!(saved["turn_incomplete"], false);
        host.shared.store.verify_thread_checkpoint(&saved).unwrap();
        assert!(host.shutdown());
    }
    #[test]
    fn durable_flush_retains_uncertain_spans_and_refuses_oversized_records() {
        let (_dir, host) = fixture("");
        let shared = &host.shared;
        let record = |text: &str| json!({"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":text}]}});
        let mut durable = DurableTurn::default();
        durable.stage(record("password='long "), shared).unwrap();
        durable.flush(shared, false).unwrap();
        assert!(!fs::read_to_string(shared.store.transcript_path())
            .unwrap()
            .contains("long "));
        durable.stage(record("secret phrase'"), shared).unwrap();
        durable.flush(shared, true).unwrap();
        assert!(fs::read_to_string(shared.store.transcript_path())
            .unwrap()
            .contains("[REDACTED:value]"));
        assert!(durable
            .stage(record(&"x".repeat(1024 * 1024)), shared)
            .is_err());
        assert!(host.shutdown());
    }
    #[test]
    fn tool_separator_keeps_recent_credential_label_context() {
        let body = r#"if row['type']=='user':
  emit({'type':'stream_event','session_id':'$SESSION','event':{'delta':{'type':'text_delta','text':'Bearer '}}})
  emit({'type':'assistant','session_id':'$SESSION','message':{'role':'assistant','content':[{'type':'tool_use','id':'tool','name':'fixture','input':{}}]}})
  emit({'type':'stream_event','session_id':'$SESSION','event':{'delta':{'type':'text_delta','text':'abcdefghijklmnopqrstuvwx '}}})
  emit({'type':'result','session_id':'$SESSION','is_error':False})
"#.replace("$SESSION", SESSION);
        let (_dir, host) = fixture(&body);
        let mut events = Vec::new();
        host.prompt("task", &mut |event| events.push(event));
        let public = serde_json::to_string(&events).unwrap();
        assert!(public.contains("[REDACTED:bearer]"));
        assert!(!public.contains("abcdefgh"));
        assert!(host.shutdown());
    }
    #[test]
    fn adjacent_provider_blocks_do_not_release_incomplete_credentials() {
        let body = r#"if row['type']=='user':
  for index,chunk in enumerate(['sk-', 'abcdefghijklmnop', 'qrstuvwxyz123456 ']):
   emit({'type':'stream_event','session_id':'$SESSION','event':{'type':'content_block_delta','index':index,'delta':{'type':'text_delta','text':chunk}}})
   emit({'type':'stream_event','session_id':'$SESSION','event':{'type':'content_block_stop','index':index}})
   emit({'type':'assistant','session_id':'$SESSION','message':{'role':'assistant','content':[{'type':'text','text':chunk}]}})
  emit({'type':'result','session_id':'$SESSION','is_error':False})
"#.replace("$SESSION", SESSION);
        let (_dir, host) = fixture(&body);
        let mut events = Vec::new();
        host.prompt("task", &mut |event| events.push(event));
        let text = events
            .iter()
            .filter(|e| e["type"] == "text_delta")
            .map(|e| e["data"]["text"].as_str().unwrap())
            .collect::<String>();
        assert_eq!(text, "[REDACTED:api-key] ");
        assert_eq!(events.last().unwrap()["data"]["is_error"], false);
        assert!(host.shutdown());
    }
    #[test]
    fn fragmented_credentials_are_private_and_public_prompt_is_visible() {
        let body = r#"if row['type']=='user':
  assert row['message']['content']=='literal task sk-abcdefghijklmnopqrstuvwxyz123456'
  for kind,field,index,parent in [('text_delta','text',0,None),('thinking_delta','thinking',1,'peer-tool')]:
   for chunk in ['safe prose password  =  ', "'long secret ", "phrase' ", 'Bearer abcdef', 'ghijklmnopqrstuvwxyz ', 'sk-abcdefghijk', 'lmnopqrstuvwxyz123456 ', 'finished']:
    emit({'type':'stream_event','session_id':'$SESSION','parent_tool_use_id':parent,'event':{'type':'content_block_delta','index':index,'delta':{'type':kind,field:chunk}}})
   emit({'type':'stream_event','session_id':'$SESSION','parent_tool_use_id':parent,'event':{'type':'content_block_stop','index':index}})
  emit({'type':'assistant','session_id':'$SESSION','message':{'role':'assistant','content':[{'type':'tool_use','id':'tool','name':'fixture','input':{}}]}})
  emit({'type':'result','session_id':'$SESSION','is_error':False})
"#.replace("$SESSION", SESSION);
        let (_dir, host) = fixture(&body);
        let mut events = Vec::new();
        host.prompt(
            "literal task sk-abcdefghijklmnopqrstuvwxyz123456",
            &mut |event| events.push(event),
        );
        assert_eq!(events[0]["type"], "turn_started");
        assert_eq!(
            events[0]["data"]["prompt"],
            "literal task [REDACTED:api-key]"
        );
        for kind in ["text_delta", "reasoning_delta"] {
            let pieces = events
                .iter()
                .filter(|e| e["type"] == kind)
                .collect::<Vec<_>>();
            assert_eq!(pieces[0]["data"]["text"], "safe prose password  =  ");
            let text = pieces
                .iter()
                .map(|e| e["data"]["text"].as_str().unwrap())
                .collect::<String>();
            assert!(text.contains("[REDACTED:value]"));
            assert!(text.contains("[REDACTED:bearer]"));
            assert!(text.contains("[REDACTED:api-key]"));
            assert!(text.ends_with("finished\n"));
            if kind == "reasoning_delta" {
                assert!(pieces.iter().all(|e| e["data"]["parent_id"] == "peer-tool"));
            }
        }
        let public = serde_json::to_string(&events).unwrap();
        for secret in ["long secret", "phrase'", "abcdefgh", "lmnopqrstuvwxyz"] {
            assert!(!public.contains(secret));
        }
        let tool = events
            .iter()
            .position(|e| e["type"] == "tool_call")
            .unwrap();
        assert!(events
            .iter()
            .enumerate()
            .filter(|(_, e)| e["type"] == "text_delta" || e["type"] == "reasoning_delta")
            .all(|(i, _)| i < tool));
        assert_eq!(events.last().unwrap()["data"]["is_error"], false);
        assert!(host.shutdown());
    }
    #[test]
    fn count_only_thinking_progress_is_live_bounded_and_resets_per_turn() {
        let (_dir, host) = fixture(&format!(
            r#"if row['type']=='user':
  for value in [12, 24, 24, 6, None, -1, 1.5, '32', 1000001, 18446744073709551616, 48]:
    emit({{'type':'stream_event','session_id':'{SESSION}','event':{{'type':'content_block_delta','delta':{{'type':'thinking_delta','thinking':'','estimated_tokens':value}}}}}})
  emit({{'type':'stream_event','session_id':'{SESSION}','event':{{'type':'content_block_delta','delta':{{'type':'signature_delta','signature':'opaque-signature-fixture','estimated_tokens':100}}}}}})
  time.sleep(.05)
  emit({{'type':'stream_event','session_id':'{SESSION}','event':{{'type':'content_block_delta','delta':{{'type':'text_delta','text':'Answer'}}}}}})
  emit({{'type':'result','session_id':'{SESSION}','is_error':False,'usage':{{'input_tokens':5,'output_tokens':2}}}})
"#
        ));
        for prompt in ["first", "second"] {
            let mut events = Vec::new();
            host.prompt(prompt, &mut |event| events.push(event));
            assert_eq!(events[0]["type"], "turn_started");
            let progress = events
                .iter()
                .filter(|event| event["type"] == "reasoning_progress")
                .collect::<Vec<_>>();
            assert_eq!(
                progress
                    .iter()
                    .map(|event| event["data"]["approx_tokens"].as_u64().unwrap())
                    .collect::<Vec<_>>(),
                vec![12, 24, 48]
            );
            assert!(progress
                .iter()
                .all(|event| event["data"]["count_is_estimate"] == true
                    && event["data"].get("text").is_none()));
            assert_eq!(events[1]["type"], "reasoning_progress");
            let text = events
                .iter()
                .position(|event| event["type"] == "text_delta")
                .unwrap();
            assert!(events
                .iter()
                .enumerate()
                .filter(|(_, event)| event["type"] == "reasoning_progress")
                .all(|(index, _)| index < text));
            assert!(!events
                .iter()
                .any(|event| event["type"] == "reasoning_delta"));
            assert!(!serde_json::to_string(&events)
                .unwrap()
                .contains("opaque-signature-fixture"));
            assert_eq!(events.last().unwrap()["type"], "turn_done");
        }
        assert!(host.shutdown());
    }
    #[test]
    fn reasoning_progress_preserves_parent_and_ignores_signatures() {
        let mut progress = ReasoningProgress::default();
        let event = progress
            .event(
                &json!({"delta":{"type":"thinking_delta","estimated_tokens":10}}),
                Some("tool"),
            )
            .unwrap();
        assert_eq!(event["data"]["parent_id"], "tool");
        assert!(progress.event(&json!({"delta":{"type":"signature_delta","estimated_tokens":20,"signature":"opaque"}}),None).is_none());
        assert_eq!(progress.tokens, 10);
        let next = progress
            .event(
                &json!({"index":1,"delta":{"type":"thinking_delta","estimated_tokens":5}}),
                Some("tool"),
            )
            .unwrap();
        assert_eq!(next["data"]["approx_tokens"], 15);
        assert!(progress
            .event(&json!({"type":"message_start"}), Some("tool"))
            .is_none());
        let next = progress
            .event(
                &json!({"index":0,"delta":{"type":"thinking_delta","estimated_tokens":2}}),
                Some("tool"),
            )
            .unwrap();
        assert_eq!(next["data"]["approx_tokens"], 17);
    }
    #[test]
    fn same_cli_turn_observes_verified_model_effort_and_immediate_reasoning() {
        let (_dir, host) = fixture(&format!(
            r#"if row['type']=='user':
  emit({{'type':'stream_event','session_id':'{SESSION}','event':{{'type':'content_block_delta','delta':{{'type':'thinking_delta','thinking':'reasoning before completion'}}}}}})
  emit({{'type':'stream_event','session_id':'{SESSION}','event':{{'type':'content_block_delta','delta':{{'type':'text_delta','text':model+' '+effort+' '+str(os.getpid())}}}}}})
  time.sleep(.05)
  emit({{'type':'assistant','session_id':'{SESSION}','message':{{'role':'assistant','content':[{{'type':'text','text':model+' '+effort}}]}}}})
  emit({{'type':'result','session_id':'{SESSION}','is_error':False,'usage':{{'input_tokens':5,'output_tokens':2}}}})
"#
        ));
        let models = host.call("list_models", &json!({})).unwrap();
        assert!(models["models"]
            .as_array()
            .unwrap()
            .contains(&json!("claude-sonnet-5")));
        assert_eq!(
            host.call("set_model", &json!({"model":"sonnet"})).unwrap()["model"],
            "claude-sonnet-5"
        );
        assert_eq!(
            host.call("set_effort", &json!({"effort":"low"})).unwrap()["verification_pending"],
            false
        );
        let mut events = Vec::new();
        host.prompt("first", &mut |e| events.push(e));
        let first = events
            .iter()
            .filter(|e| e["type"] == "text_delta")
            .map(|e| e["data"]["text"].as_str().unwrap())
            .collect::<String>();
        assert!(first.starts_with("claude-sonnet-5 low "));
        assert_eq!(events[1]["type"], "reasoning_delta");
        assert_eq!(events.last().unwrap()["data"]["ctx_tokens"], 1234);
        host.call("set_effort", &json!({"effort":"high"})).unwrap();
        let mut second = Vec::new();
        host.prompt("second", &mut |e| second.push(e));
        let text = second
            .iter()
            .filter(|e| e["type"] == "text_delta")
            .map(|e| e["data"]["text"].as_str().unwrap())
            .collect::<String>();
        assert!(text.starts_with("claude-sonnet-5 high "));
        assert_eq!(
            first.split_whitespace().last(),
            text.split_whitespace().last()
        );
        assert_eq!(
            host.call("set_permission_mode", &json!({"mode":"default"}))
                .unwrap()["mode"],
            "default"
        );
        let old = host.shared.store.read_thread().unwrap().unwrap();
        assert_eq!(old["thread_id"], SESSION);
        assert_eq!(old["turn_incomplete"], false);
        host.shared.store.verify_thread_checkpoint(&old).unwrap();
        assert!(host.shutdown());
    }
    #[test]
    fn exact_permission_and_ask_answers_retain_original_input_and_resolve_once() {
        let (_dir, host) = fixture(&format!(
            r#"if row['type']=='user':
  emit({{'type':'control_request','request_id':'exact-request-id','request':{{'subtype':'can_use_tool','tool_use_id':'exact-tool-id','tool_name':'AskUserQuestion','input':{{'questions':[{{'question':'Choose','options':[{{'label':'A'}}]}}],'opaque':'original'}}}}}})
 elif row['type']=='control_response':
  assert row['response']['request_id']=='exact-request-id'
  answer=row['response']['response']
  assert answer['behavior']=='allow' and answer['toolUseID']=='exact-tool-id'
  assert answer['updatedInput']=={{'questions':[{{'question':'Choose','options':[{{'label':'A'}}]}}],'opaque':'original','answers':{{'Choose':'A'}}}}
  emit({{'type':'assistant','session_id':'{SESSION}','message':{{'role':'assistant','content':[{{'type':'text','text':'answered'}}]}}}})
  emit({{'type':'result','session_id':'{SESSION}','is_error':False}})
"#
        ));
        let mut asked = false;
        host.prompt("question", &mut |event| {
            if event["type"] == "needs_input" {
                asked = true;
                assert_eq!(event["data"]["id"], "exact-request-id");
                assert!(host.call("set_effort", &json!({"effort":"low"})).is_err());
                assert_eq!(
                    host.call(
                        "answer_needs_input",
                        &json!({"id":"foreign","answer":{"decision":"allow"}})
                    )
                    .unwrap()["applied"],
                    false
                );
                assert_eq!(
                    host.call(
                        "answer_needs_input",
                        &json!({"id":"exact-request-id","answer":{"answers":{"Choose":"A"}}})
                    )
                    .unwrap()["applied"],
                    true
                );
                assert_eq!(
                    host.call(
                        "answer_needs_input",
                        &json!({"id":"exact-request-id","answer":{"answers":{"Choose":"A"}}})
                    )
                    .unwrap()["applied"],
                    false
                );
            }
        });
        assert!(asked);
        assert!(host.shutdown());
    }
    #[test]
    fn quota_nested_windows_keep_limiting_status_on_its_own_window() {
        let limits = rate_limits(&json!({"status":"allowed_warning","rateLimitType":"five_hour","resetsAt":123,
            "unifiedWindows":{"five_hour":{"utilization":0.23,"resetsAt":123},"seven_day":{"utilization":0.47,"resetsAt":456}},"raw":"private"}));
        let (_dir, host) = fixture("");
        for limit in limits {
            let _ = update_quota(&host.shared, &limit);
        }
        let billing = host.billing_snapshot().unwrap();
        assert_eq!(billing["quota"], "5h:23% week:47%");
        assert_eq!(billing["quota_limits"]["five_hour"]["status"], "allowed_warning");
        assert!(billing["quota_limits"]["seven_day"].get("status").is_none());
        assert_eq!(billing["quota_source"], "claude_cli");
        assert!(!billing.to_string().contains("private"));
        assert!(host.shutdown());
    }
    #[test]
    fn quota_nested_only_and_partial_updates_preserve_other_windows() {
        let (_dir, host) = fixture("");
        for payload in [
            json!({"unifiedWindows":{"five_hour":{"utilization":0.1,"resetsAt":123},"seven_day":{"utilization":0.2,"resetsAt":456}}}),
            json!({"unifiedWindows":{"five_hour":{"utilization":0.3}}}),
            json!({"status":"allowed","rateLimitType":"five_hour","resetsAt":789}),
        ] {
            for limit in rate_limits(&payload) {
                let _ = update_quota(&host.shared, &limit);
            }
        }
        let billing = host.billing_snapshot().unwrap();
        assert_eq!(billing["quota"], "week:20%");
        assert!(billing["quota_limits"]["five_hour"].get("percent").is_none());
        assert_eq!(billing["quota_limits"]["five_hour"]["resets_at"], 789);
        assert_eq!(billing["quota_limits"]["seven_day"]["resets_at"], 456);
        // Refreshing the catalog/account must not clear observed quotas.
        set_billing_account(&host.shared, &json!({"subscriptionType":"max"}));
        assert_eq!(host.billing_snapshot().unwrap()["quota"], "week:20%");
        assert!(host.shutdown());
    }
    #[test]
    fn quota_reset_advance_clears_previous_percent_until_reported() {
        let (_dir, host) = fixture("");
        for payload in [
            json!({"status":"allowed","rateLimitType":"five_hour","utilization":0.3,"resetsAt":123}),
            json!({"unifiedWindows":{"five_hour":{"resetsAt":456}}}),
        ] {
            for limit in rate_limits(&payload) {
                let _ = update_quota(&host.shared, &limit);
            }
        }
        let billing = host.billing_snapshot().unwrap();
        assert!(billing["quota"].is_null());
        assert!(billing["quota_limits"]["five_hour"].get("percent").is_none());
        assert!(billing["quota_limits"]["five_hour"].get("status").is_none());
        for payload in [
            json!({"unifiedWindows":{"five_hour":{"utilization":0.1}}}),
            json!({"status":"allowed_warning","rateLimitType":"five_hour","resetsAt":456}),
        ] {
            for limit in rate_limits(&payload) {
                let _ = update_quota(&host.shared, &limit);
            }
        }
        let billing = host.billing_snapshot().unwrap();
        assert_eq!(billing["quota"], "5h:10%");
        assert_eq!(billing["quota_limits"]["five_hour"]["status"], "allowed_warning");
        assert!(host.shutdown());
    }
    #[test]
    fn quota_invalid_window_values_do_not_poison_valid_siblings() {
        for invalid in [json!(-0.1),json!(1.01),json!(f64::NAN),json!(f64::INFINITY),json!("0.2"),Value::Null] {
            let limits = rate_limits(&json!({"unifiedWindows":{"five_hour":{"utilization":invalid,"resetsAt":123},"seven_day":{"utilization":0.4,"resetsAt":456}}}));
            assert_eq!(limits.len(), 1);
            assert_eq!(limits[0]["window"], "seven_day");
        }
        for invalid in [json!(-1),json!(1.5),json!(253402300800u64),json!("123"),Value::Null] {
            assert!(rate_limits(&json!({"status":"allowed","rateLimitType":"five_hour","utilization":0.2,"resetsAt":invalid})).is_empty());
        }
        assert!(rate_limits(&json!({"status":"invented","rateLimitType":"five_hour","utilization":0.2})).is_empty());
        assert!(rate_limits(&json!({"unifiedWindows":{"five_hour":{},"unknown":{"utilization":0.2}}})).is_empty());
    }
    #[test]
    fn quota_overage_windows_are_separate_and_do_not_invent_status() {
        let (_dir, host) = fixture("");
        for payload in [
            json!({"unifiedWindows":{"seven_day_overage_included":{"utilization":0.1,"resetsAt":123}}}),
            json!({"status":"allowed","rateLimitType":"overage","utilization":0.2,"resetsAt":456}),
        ] {
            for limit in rate_limits(&payload) {
                let _ = update_quota(&host.shared, &limit);
            }
        }
        let billing = host.billing_snapshot().unwrap();
        assert_eq!(billing["quota"], "included:10% extra:20%");
        assert!(billing["quota_limits"]["seven_day_overage_included"].get("status").is_none());
        assert_eq!(billing["quota_limits"]["overage"]["status"], "allowed");
        assert!(host.shutdown());
    }
    #[test]
    fn quota_startup_event_survives_control_and_late_account_initialization() {
        let startup = "emit({'type':'rate_limit_event','rate_limit_info':{'unifiedWindows':{'five_hour':{'utilization':0.23,'resetsAt':123},'seven_day':{'utilization':0.47,'resetsAt':456}}}}) if sub=='initialize' else None";
        let (_dir, host) = fixture_with_startup("", startup, None);
        // Production learns account billing only after initialize/get_settings.
        assert!(host.billing_snapshot().is_none());
        set_billing_account(&host.shared, &json!({"subscriptionType":"max"}));
        assert_eq!(host.billing_snapshot().unwrap()["quota"], "5h:23% week:47%");
        assert!(host.call("set_effort", &json!({"effort":"low"})).unwrap()["verified"] == true);
        assert!(host.shutdown());
    }
    #[test]
    fn quota_projection_never_forwards_credentials_or_invalid_percent() {
        let (_dir, host) = fixture("");
        let report=rate_limits(&json!({"status":"allowed_warning","rateLimitType":"five_hour","utilization":0.23,"resetsAt":123,"raw":"private"})).remove(0);
        assert!(report.get("raw").is_none());
        assert_eq!(
            update_quota(&host.shared, &report).unwrap()["quota"],
            "5h:23%"
        );
        assert!(rate_limits(
            &json!({"status":"allowed","rateLimitType":"five_hour","utilization":1.01})
        ).is_empty());
        assert!(host.shutdown());
    }
}

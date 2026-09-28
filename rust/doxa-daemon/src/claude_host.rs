//! Native Claude Code host: one CLI owner, immediate stream events, exact control replies.
use doxa_claude::{Cli, CliOptions, Error};
use doxa_lore::LoreClient;
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
    selection: Mutex<(Option<String>, Option<String>, String)>,
    account: Mutex<Option<Value>>,
    billing: Mutex<Option<Value>>,
    catalog: Mutex<Value>,
    agent: Option<Arc<crate::agent_tools::AgentTools>>,
    peer: Mutex<Option<PeerToolHandler>>,
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
        if !self.enabled || self.failed.load(Ordering::Acquire) {
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
        if self.peer_allowed {
            rows.extend(doxa_engines::peer_tools::definitions());
        }
        rows.into_iter().map(|r|json!({"name":r["name"].as_str().unwrap().strip_prefix("mcp__doxa__").unwrap(),"description":r["description"],"inputSchema":r["inputSchema"]})).collect()
    }
    fn tool(&self, name: &str, args: &Value) -> Result<Value, String> {
        let wire = format!("mcp__doxa__{name}");
        if let Some(agent) = &self.agent {
            if agent.contains(&wire) {
                return agent.call(&wire, args);
            }
        }
        let method = doxa_engines::peer_tools::rpc(&wire, args).map_err(str::to_owned)?;
        (self
            .peer
            .lock()
            .map_err(|_| "peer tools unavailable")?
            .clone()
            .ok_or("peer tools unavailable")?)(method, args)
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
            Ok(frame) if matches!(frame["type"].as_str(), Some("system" | "rate_limit_event")) => {}
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
            let old = store
                .read_thread()
                .map_err(|_| "Claude saved state unavailable")?
                .ok_or("Claude resume metadata is unavailable")?;
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
            selection: Mutex::new((
                requested_model.clone(),
                requested_effort.clone(),
                mode.clone(),
            )),
            account: Mutex::new(None),
            billing: Mutex::new(None),
            catalog: Mutex::new(Value::Null),
            agent: crate::agent_tools::AgentTools::new(
                &cwd,
                session_id,
                "claude",
                enabled,
            ),
            peer: Mutex::new(None),
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
            *shared.billing.lock().unwrap() = billing_from_account(account);
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
                if self.has_active_work() || self.shared.closing.load(Ordering::Acquire) {
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
fn broker(mut cli: Cli, commands: Receiver<Command>, shared: Arc<Shared>) {
    let mut operations: HashMap<String, Operation> = HashMap::new();
    let mut inputs: HashMap<String, PendingInput> = HashMap::new();
    let mut events = None;
    let mut overflow = false;
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
                    events = Some(sink);
                    overflow = false;
                    compact_permit = text.trim() == "/compact";
                    let ready = !shared.cancelled.load(Ordering::Acquire);
                    let admitted = (|| {
                        if !ready {
                            return Err("Claude prompt cancelled before admission".into());
                        }
                        shared.persist(json!({"type":"user","message":{"role":"user","content":text},"cwd":shared.cwd,"sessionId":shared.session,"timestamp":crate::iso_now()}))?;
                        shared.checkpoint(true)?;
                        cli.prompt(&text, &shared.session)
                            .map_err(|_| "Claude prompt write failed".to_owned())
                    })();
                    if let Err(reason) = admitted {
                        let _ = reply.send(Err(reason.clone()));
                        send_event(&events, done(&reason));
                        events = None;
                        shared.active.store(false, Ordering::Release);
                    } else {
                        send_event(&events, json!({"type":"turn_started","data":{}}));
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
                done("Claude cancellation deadline expired; provider terminated"),
            );
            shared.active.store(false, Ordering::Release);
            return;
        }
        let frame = match cli.recv(POLL) {
            Ok(v) => v,
            Err(Error::Timeout) => continue,
            Err(_) => {
                send_event(&events, done("Claude CLI stream closed"));
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
        match frame["type"].as_str() {
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
                            *shared.billing.lock().unwrap() = billing_from_account(&account);
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
                let delta = &event["delta"];
                if let Some((kind, text)) = match delta["type"].as_str() {
                    Some("text_delta") => delta["text"].as_str().map(|s| ("text_delta", s)),
                    Some("thinking_delta") => {
                        delta["thinking"].as_str().map(|s| ("reasoning_delta", s))
                    }
                    _ => None,
                } {
                    let text = match shared.scrub(text) {
                        Ok(s) => s,
                        Err(_) => {
                            let _ = cli.control(json!({"subtype":"interrupt"}));
                            continue;
                        }
                    };
                    let mut data = json!({"text":text});
                    if let Some(parent) = frame["parent_tool_use_id"].as_str() {
                        data["parent_id"] = json!(parent)
                    }
                    if !overflow && !send_event(&events, json!({"type":kind,"data":data})) {
                        overflow = true;
                        let _ = cli.control(json!({"subtype":"interrupt"}));
                        turn_deadline = Some(Instant::now() + Duration::from_secs(5));
                    }
                }
            }
            Some("assistant" | "user") => {
                let kind = frame["type"].as_str().unwrap();
                if frame["message"].is_object() {
                    let mut message = frame["message"].clone();
                    if let Some(blocks) = message["content"].as_array_mut() {
                        blocks.retain(|b| {
                            !matches!(b["type"].as_str(), Some("thinking" | "redacted_thinking"))
                        });
                    }
                    if shared.persist(json!({"type":kind,"message":message,"sessionId":shared.session,"cwd":shared.cwd,"timestamp":crate::iso_now()})).is_err(){let _=cli.control(json!({"subtype":"interrupt"}));}
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
                let error =
                    frame["is_error"] == true || overflow || shared.failed.load(Ordering::Acquire);
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
                let data = json!({"ctx_percentage":null,"ctx_tokens":null,"ctx_max_tokens":null,"input_tokens":usage["input_tokens"],"output_tokens":usage["output_tokens"],"cache_read_input_tokens":usage["cache_read_input_tokens"],"cache_creation_input_tokens":usage["cache_creation_input_tokens"],"cost_usd":frame["total_cost_usd"],"is_error":error||shared.failed.load(Ordering::Acquire),"num_turns":frame["num_turns"],"error":if overflow{Some("Claude event stream overflowed; turn cancelled")}else if error{Some("Claude turn failed or was interrupted")}else{None}});
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
                if let Some(projected) = rate_limit(data) {
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
fn rate_limit(value: &Value) -> Option<Value> {
    let window = value["rateLimitType"].as_str().filter(|s| {
        matches!(
            *s,
            "five_hour" | "seven_day" | "seven_day_opus" | "seven_day_sonnet"
        )
    })?;
    let status = value["status"]
        .as_str()
        .filter(|s| matches!(*s, "allowed" | "allowed_warning" | "rejected"))?;
    let mut result = json!({"window":window,"status":status});
    if let Some(utilization) = value.get("utilization") {
        let utilization = utilization
            .as_f64()
            .filter(|v| v.is_finite() && (0.0..=1.0).contains(v))?;
        result["percent"] = json!((utilization * 100.0).round() as u64);
    }
    if let Some(reset) = value.get("resetsAt") {
        result["resets_at"] = json!(reset.as_u64().filter(|v| *v <= 253402300799)?);
    }
    Some(result)
}
fn update_quota(shared: &Shared, limit: &Value) -> Option<Value> {
    let mut cached = shared.billing.lock().ok()?;
    let billing = cached.as_mut()?;
    if billing["mode"] != "subscription" {
        return None;
    }
    if !billing["quota_limits"].is_object() {
        billing["quota_limits"] = json!({});
    }
    let window = limit["window"].as_str()?;
    let mut row = limit.clone();
    row.as_object_mut()?.remove("window");
    row["source"] = json!("sdk");
    row["stale"] = json!(false);
    billing["quota_limits"][window] = row;
    let mut text = Vec::new();
    for (key, label) in [
        ("five_hour", "5h"),
        ("seven_day", "week"),
        ("seven_day_opus", "opus"),
        ("seven_day_sonnet", "sonnet"),
    ] {
        if let Some(percent) = billing["quota_limits"][key]["percent"].as_u64() {
            text.push(format!("{label}:{percent}%"));
        }
    }
    billing["quota"] = if text.is_empty() {
        Value::Null
    } else {
        json!(text.join(" "))
    };
    billing["quota_source"] = json!("sdk");
    billing["quota_stale"] = json!(false);
    Some(billing.clone())
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
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("claude");
        fs::write(&path,format!(r#"#!/usr/bin/python3
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
            selection: Mutex::new((
                Some("claude-opus-5-5".into()),
                Some("high".into()),
                "default".into(),
            )),
            account: Mutex::new(None),
            billing: Mutex::new(Some(
                json!({"mode":"subscription","type":"max","quota":null}),
            )),
            catalog: Mutex::new(Value::Null),
            agent: None,
            peer: Mutex::new(None),
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
        let first = events.iter().find(|e| e["type"] == "text_delta").unwrap()["data"]["text"]
            .as_str()
            .unwrap()
            .to_owned();
        assert!(first.starts_with("claude-sonnet-5 low "));
        assert_eq!(events[1]["type"], "reasoning_delta");
        assert_eq!(events.last().unwrap()["data"]["ctx_tokens"], 1234);
        host.call("set_effort", &json!({"effort":"high"})).unwrap();
        let mut second = Vec::new();
        host.prompt("second", &mut |e| second.push(e));
        let text = second.iter().find(|e| e["type"] == "text_delta").unwrap()["data"]["text"]
            .as_str()
            .unwrap();
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
    fn quota_projection_never_forwards_credentials_or_invalid_percent() {
        let (_dir, host) = fixture("");
        let report=rate_limit(&json!({"status":"allowed_warning","rateLimitType":"five_hour","utilization":0.23,"resetsAt":123,"raw":"private"})).unwrap();
        assert!(report.get("raw").is_none());
        assert_eq!(
            update_quota(&host.shared, &report).unwrap()["quota"],
            "5h:23%"
        );
        assert!(rate_limit(
            &json!({"status":"allowed","rateLimitType":"five_hour","utilization":1.01})
        )
        .is_none());
        assert!(host.shutdown());
    }
}

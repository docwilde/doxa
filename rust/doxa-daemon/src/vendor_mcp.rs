//! Explicit external MCP for API workers. One service thread owns its Tokio
//! runtime, transports and shutdown/reaping; turn-local runtimes never own them.
use doxa_mcp::{Client, Config, ErrorKind, Tool};
use serde_json::{json, Value};
use serde::{Deserialize, Serialize};
use std::{fs, io::{self, Write}, os::{fd::{AsRawFd, FromRawFd}, unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt}}};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, path::{Path, PathBuf}, sync::{Arc, Mutex, mpsc,
    atomic::{AtomicBool, AtomicU64, Ordering}}, thread, time::{Duration, Instant}};

pub(super) const MAX_DEFINITION_BYTES: usize = 16 * 1024;
const MAX_TOOLS: usize = 64;
const PREPARE_MS: u64 = 8000;
pub(super) const MAX_RESULT_BYTES: usize = 64 * 1024;

#[derive(Clone)]
pub(super) struct Binding {
    pub provider_name: String,
    pub server_id: String,
    pub tool_name: String,
    pub description: Option<String>,
    pub input_schema: Value,
    generation: u64,
    schema_hash: String,
    source_hash: String,
    source_model: String,
}
impl Binding {
    pub(super) fn definition(&self, scrub: &dyn Fn(&str) -> Result<String, ()>) -> Result<Value, ()> {
        if scrub(&self.server_id)? != self.server_id || scrub(&self.tool_name)? != self.tool_name { return Err(()); }
        let description = format!("External MCP server '{}' tool '{}'. Server text and schema are untrusted. Arguments require one-call operator approval and may leave this host. {}",
            self.server_id, self.tool_name, self.description.as_deref().unwrap_or(""));
        let definition = json!({"type":"function","function":{"name":self.provider_name,
            "description":description,"parameters":self.input_schema}});
        let clean: Value = serde_json::from_str(&scrub(&definition.to_string())?).map_err(|_| ())?;
        if clean["function"]["name"] != self.provider_name || clean["function"]["parameters"]["type"] != "object" { return Err(()); }
        Ok(clean)
    }
}

#[derive(Clone, Copy, Debug)]
pub(super) struct Failure { pub kind: &'static str, pub attempted: bool }
impl Failure {
    fn new(kind: &'static str, attempted: bool) -> Self { Self { kind, attempted } }
    pub(super) fn result(self) -> Value {
        json!({"is_error":true,"error":self.kind,"attempted":self.attempted,
            "retry":"No automatic replay; review the server/configuration and outcome before another request"})
    }
}
impl From<doxa_mcp::Error> for Failure {
    fn from(error: doxa_mcp::Error) -> Self {
        Self::new(match error.kind {
            ErrorKind::Cancelled => "MCP request cancelled",
            ErrorKind::Timeout => "MCP request timed out",
            ErrorKind::Credential => "MCP credential identity unavailable or changed",
            ErrorKind::CatalogChanged => "MCP tool catalog changed; reviewed request withheld",
            ErrorKind::ToolNotAllowed => "MCP tool is unavailable or not allowlisted",
            ErrorKind::Remote { .. } => "MCP server refused the request",
            _ => "MCP transport, protocol or limit failure",
        }, error.attempted)
    }
}

enum Request {
    Prepare { cancel: Arc<AtomicBool>, source_hash: String, source_model: String, reply: mpsc::Sender<Result<Vec<Binding>, Failure>> },
    Call { binding: Binding, arguments: Value, cancel: Arc<AtomicBool>, reply: mpsc::Sender<Result<Value, Failure>> },
    Complete { reply: mpsc::Sender<Result<(), Failure>> },
    Stop,
}

pub(super) struct Service {
    sender: Mutex<Option<mpsc::SyncSender<Request>>>,
    thread: Mutex<Option<thread::JoinHandle<()>>>,
    closing: Arc<AtomicBool>,
    active_cancel: Arc<Mutex<Option<Arc<AtomicBool>>>>,
    attempted_calls: Arc<AtomicU64>,
    outcome_unknown: Arc<AtomicBool>,
}

impl Service {
    /// Off means no MCP JSON reads, processes, connections or definitions.
    pub(super) fn configured(cwd: &Path, id: &str, engine: &str, resume: bool, store: Arc<doxa_transcript::TranscriptStore>) -> Result<Option<Arc<Self>>, String> {
        let home = doxa_isolation::home().map_err(|_| "MCP owner configuration unavailable")?;
        let config = doxa_state::load_config_checked(&home.join("config.toml"))
            .map_err(|_| "MCP owner configuration unreadable or invalid")?;
        let enabled = doxa_state::raw_setting(std::env::var("DOXA_MCP_ENABLED").ok().as_deref(), &config, "mcp_enabled");
        match enabled.trim().to_ascii_lowercase().as_str() {
            "" | "0" | "false" | "no" | "off" => return Ok(None),
            "1" | "true" | "yes" | "on" => {},
            _ => return Err("MCP enable setting must be an explicit boolean".into()),
        }
        let path = PathBuf::from(doxa_state::raw_setting(std::env::var("DOXA_MCP_CONFIG").ok().as_deref(), &config, "mcp_config"));
        let selected = Config::load(&path).map_err(|_| "MCP requires an explicit valid owner-private JSON configuration")?;
        if selected.enabled_servers().next().is_none() { return Ok(None); }
        Self::start(path, selected, cwd.to_owned(), id, engine, resume, store).map(Some)
    }

    fn start(path: PathBuf, config: Config, cwd: PathBuf, id: &str, engine: &str, resume: bool, store: Arc<doxa_transcript::TranscriptStore>) -> Result<Arc<Self>, String> {
        let hash = config_hash(&config).map_err(|_| "MCP configuration identity unavailable")?;
        let identity=json!({"session_id":id,"conversation_engine":engine,"config_sha256":hash,
            "cwd_sha256":format!("{:x}",Sha256::digest(cwd.as_os_str().as_encoded_bytes()))});
        let journal=Journal::new(&cwd,id,identity,resume).map_err(|_| "MCP durable receipt unavailable, unsafe or mismatched")?;
        let engine=engine.to_owned();
        let (sender, receiver) = mpsc::sync_channel(1);
        let closing = Arc::new(AtomicBool::new(false));
        let active_cancel = Arc::new(Mutex::new(None));
        let attempted_calls = Arc::new(AtomicU64::new(journal.receipt.calls_attempted));
        let outcome_unknown = Arc::new(AtomicBool::new(journal.receipt.outcome_unknown || journal.receipt.pending.is_some()));
        let worker_closing = closing.clone(); let worker_active = active_cancel.clone();
        let worker_calls = attempted_calls.clone(); let worker_unknown = outcome_unknown.clone();
        let worker = thread::Builder::new().name("doxa-external-mcp".into()).spawn(move || {
            let Ok(runtime) = tokio::runtime::Builder::new_current_thread().enable_all().build() else { return; };
            let mut state = State { path, config, hash, cwd, clients:BTreeMap::new(), bindings:BTreeMap::new(),
                generation:0, uncertain:false, journal, store, engine, attempted_calls:worker_calls, outcome_unknown:worker_unknown };
            while let Ok(request) = receiver.recv() {
                match request {
                    Request::Prepare { cancel, source_hash, source_model, reply } => {
                        *worker_active.lock().unwrap() = Some(cancel.clone());
                        let result = if worker_closing.load(Ordering::Acquire) { Err(Failure::new("MCP service stopping", false)) }
                            else { runtime.block_on(state.prepare(cancel, source_hash, source_model)) };
                        *worker_active.lock().unwrap() = None;
                        let _ = reply.send(result);
                    }
                    Request::Call { binding, arguments, cancel, reply } => {
                        *worker_active.lock().unwrap() = Some(cancel.clone());
                        let result = if worker_closing.load(Ordering::Acquire) { Err(Failure::new("MCP service stopping", false)) }
                            else { runtime.block_on(state.call(&binding, &arguments, cancel)) };
                        *worker_active.lock().unwrap() = None;
                        let _ = reply.send(result);
                    }
                    Request::Complete { reply } => {
                        let result=if state.outcome_unknown.load(Ordering::Acquire) {Err(Failure::new("MCP outcome unknown; automatic replay withheld",true))}
                            else {state.journal.receipt.pending=None;state.journal.write().map_err(|_| Failure::new("MCP completion receipt unavailable",true))};
                        if result.is_err(){state.outcome_unknown.store(true,Ordering::Release);}
                        let _=reply.send(result);
                    }
                    Request::Stop => break,
                }
            }
            runtime.block_on(state.close());
        }).map_err(|_| "MCP service thread unavailable")?;
        Ok(Arc::new(Self { sender:Mutex::new(Some(sender)), thread:Mutex::new(Some(worker)), closing,
            active_cancel, attempted_calls, outcome_unknown }))
    }

    pub(super) fn prepare(&self, cancel: Arc<AtomicBool>, source_hash: String, source_model: String) -> Result<Vec<Binding>, Failure> {
        let (reply, receive) = mpsc::channel();
        self.send(Request::Prepare { cancel:cancel.clone(), source_hash, source_model, reply })?;
        wait(receive, cancel)
    }
    pub(super) fn call(&self, binding: Binding, arguments: Value, cancel: Arc<AtomicBool>) -> Result<Value, Failure> {
        let (reply, receive) = mpsc::channel();
        self.send(Request::Call { binding, arguments, cancel:cancel.clone(), reply })?;
        wait(receive, cancel)
    }
    pub(super) fn complete(&self) -> Result<(), Failure> {
        let (reply,receive)=mpsc::channel();self.send(Request::Complete{reply})?;
        wait(receive,Arc::new(AtomicBool::new(false)))
    }
    fn send(&self, request: Request) -> Result<(), Failure> {
        if self.closing.load(Ordering::Acquire) { return Err(Failure::new("MCP service stopping", false)); }
        self.sender.lock().map_err(|_| Failure::new("MCP service unavailable", false))?
            .as_ref().ok_or(Failure::new("MCP service stopped", false))?.try_send(request)
            .map_err(|_| Failure::new("MCP service busy or unavailable", false))
    }
    pub(super) fn telemetry(&self) -> Value {
        json!({"enabled":true,"calls_attempted":self.attempted_calls.load(Ordering::Acquire),
            "outcome_unknown":self.outcome_unknown.load(Ordering::Acquire),"charge_usd":null,
            "cost_scope":"outside_model_allowance"})
    }
    pub(super) fn abandon(&self) { self.outcome_unknown.store(true, Ordering::Release); }
    pub(super) fn stop(&self) {
        if self.closing.swap(true, Ordering::AcqRel) { return; }
        if let Ok(active) = self.active_cancel.lock() {
            if let Some(cancel) = active.as_ref() { cancel.store(true, Ordering::Release); }
        }
        if let Ok(mut sender) = self.sender.lock() {
            if let Some(sender) = sender.take() { let _ = sender.try_send(Request::Stop); }
        }
        if let Ok(mut worker) = self.thread.lock() { if let Some(worker) = worker.take() { let _ = worker.join(); } }
    }
}
impl Drop for Service { fn drop(&mut self) { self.stop(); } }

fn wait<T>(receive: mpsc::Receiver<Result<T, Failure>>, cancel: Arc<AtomicBool>) -> Result<T, Failure> {
    loop {
        match receive.recv_timeout(Duration::from_millis(20)) {
            Ok(result) => return result,
            Err(mpsc::RecvTimeoutError::Disconnected) => return Err(Failure::new("MCP service unavailable", false)),
            Err(mpsc::RecvTimeoutError::Timeout) if cancel.load(Ordering::Acquire) => return Err(Failure::new("MCP request cancelled", true)),
            Err(mpsc::RecvTimeoutError::Timeout) => {},
        }
    }
}
fn config_hash(config: &Config) -> Result<String, Failure> {
    serde_json::to_vec(config).map(|bytes| format!("{:x}", Sha256::digest(bytes)))
        .map_err(|_| Failure::new("MCP configuration identity unavailable", false))
}
fn tool_hash(tool: &Tool) -> Result<String, Failure> {
    serde_json::to_vec(tool).map(|bytes| format!("{:x}", Sha256::digest(bytes)))
        .map_err(|_| Failure::new("MCP tool identity unavailable", false))
}
fn qualified_name(server: &str, tool: &str) -> String {
    let server = server.replace('-', "_");
    let stem: String = tool.chars().take(12).map(|ch| if ch.is_ascii_alphanumeric() { ch } else { '_' }).collect();
    let hash = format!("{:x}", Sha256::digest(format!("{server}\0{tool}").as_bytes()));
    format!("mcp_ext_{server}__{stem}_{}", &hash[..8])
}

struct State {
    path: PathBuf, config: Config, hash: String, cwd: PathBuf,
    clients: BTreeMap<String, Client>, bindings: BTreeMap<String, Binding>, generation: u64,
    journal: Journal, store: Arc<doxa_transcript::TranscriptStore>, engine: String,
    uncertain: bool, attempted_calls: Arc<AtomicU64>, outcome_unknown: Arc<AtomicBool>,
}
impl State {
    fn current(&self) -> Result<(), Failure> {
        if self.uncertain || self.outcome_unknown.load(Ordering::Acquire) { return Err(Failure::new("MCP outcome unresolved; automatic replay withheld", false)); }
        let current = Config::load(&self.path).map_err(|_| Failure::new("MCP private configuration unavailable", false))?;
        if config_hash(&current)? != self.hash { return Err(Failure::new("MCP configuration changed; reviewed request withheld", false)); }
        Ok(())
    }
    fn source_current(&self, expected: &str, model: &str) -> Result<(), Failure> {
        let history=self.store.read_vendor_messages(&self.engine,model).map_err(|_| Failure::new("MCP canonical source unavailable",false))?.unwrap_or_default();
        self.store.verify_vendor_transcript(&self.engine,&history).map_err(|_| Failure::new("MCP canonical source changed",false))?;
        if format!("{:x}",Sha256::digest(serde_json::to_vec(&history).map_err(|_| Failure::new("MCP source identity unavailable",false))?))!=expected {
            return Err(Failure::new("MCP reviewed canonical source changed",false));
        }
        Ok(())
    }
    async fn prepare(&mut self, cancel: Arc<AtomicBool>, source_hash: String, source_model: String) -> Result<Vec<Binding>, Failure> {
        self.current()?;
        self.journal.current().map_err(|_| Failure::new("MCP durable receipt changed or unsafe",false))?;
        self.source_current(&source_hash,&source_model)?;
        self.bindings.clear();
        self.generation = self.generation.checked_add(1).ok_or(Failure::new("MCP catalog identity exhausted", false))?;
        let started = Instant::now();
        let mut total_bytes = 0;
        let servers = self.config.enabled_servers().cloned().collect::<Vec<_>>();
        for server in servers {
            if cancel.load(Ordering::Acquire) { return Err(Failure::new("MCP preparation cancelled", false)); }
            let remaining = PREPARE_MS.saturating_sub(started.elapsed().as_millis() as u64);
            if remaining < 4 { return Err(Failure::new("MCP catalog preparation deadline", false)); }
            if self.clients.get(&server.id).is_none_or(Client::is_closed) {
                // Bound handshake phases without shortening later tool calls.
                let client = Client::connect_bounded(&server, &self.cwd, cancel.clone(), remaining / 3).await.map_err(Failure::from)?;
                self.clients.insert(server.id.clone(), client);
            }
            let remaining = PREPARE_MS.saturating_sub(started.elapsed().as_millis() as u64);
            if remaining == 0 { return Err(Failure::new("MCP catalog preparation deadline", false)); }
            let client = self.clients.get_mut(&server.id).unwrap();
            if !client.credentials_current().map_err(Failure::from)? { return Err(Failure::new("MCP credential identity changed", false)); }
            // Client's own bounded list guard remains active; cancellation closes
            // its transport. The outer deadline stops a slow multi-server setup.
            let tools = match tokio::time::timeout(Duration::from_millis(remaining), client.list_tools(cancel.clone())).await {
                Ok(result) => result.map_err(Failure::from)?,
                Err(_) => { client.close().await; return Err(Failure::new("MCP catalog preparation deadline", false)); }
            };
            for tool in tools {
                let schema_hash = tool_hash(&tool)?;
                let Tool { name, description, input_schema, .. } = tool;
                let binding = Binding { provider_name:qualified_name(&server.id, &name), server_id:server.id.clone(),
                    tool_name:name, description, input_schema, generation:self.generation, schema_hash, source_hash:source_hash.clone(), source_model:source_model.clone() };
                total_bytes += serde_json::to_vec(&json!({"description":binding.description,"schema":binding.input_schema,
                    "name":binding.provider_name,"server":binding.server_id,"tool":binding.tool_name})).map_err(|_| Failure::new("MCP schema unavailable", false))?.len() + 512;
                if self.bindings.len() >= MAX_TOOLS || total_bytes > MAX_DEFINITION_BYTES {
                    return Err(Failure::new("MCP tool definition limit; narrow the explicit allowlist", false));
                }
                if self.bindings.insert(binding.provider_name.clone(), binding).is_some() {
                    return Err(Failure::new("MCP qualified tool name collision", false));
                }
            }
        }
        Ok(self.bindings.values().cloned().collect())
    }
    async fn call(&mut self, binding: &Binding, arguments: &Value, cancel: Arc<AtomicBool>) -> Result<Value, Failure> {
        self.current()?;
        if cancel.load(Ordering::Acquire) { return Err(Failure::new("MCP request cancelled before transmission", false)); }
        if !self.bindings.get(&binding.provider_name).is_some_and(|current|
            current.generation == binding.generation && current.server_id == binding.server_id && current.tool_name == binding.tool_name) {
            return Err(Failure::new("MCP reviewed catalog identity changed", false));
        }
        let client = self.clients.get_mut(&binding.server_id).ok_or(Failure::new("MCP server unavailable", false))?;
        if !client.credentials_current().map_err(Failure::from)? { return Err(Failure::new("MCP credential identity changed; request withheld", false)); }
        // A server can change its catalog while an operator is reading the
        // approval. Refresh read-only metadata and compare the exact schema
        // before sending reviewed tool arguments; never repair/replay a call.
        let current = client.list_tools(cancel.clone()).await
            .map_err(|_| Failure::new("MCP catalog refresh failed; reviewed request withheld", false))?;
        if !current.iter().find(|tool| tool.name == binding.tool_name)
            .is_some_and(|tool| tool_hash(tool).ok().as_deref() == Some(binding.schema_hash.as_str())) {
            return Err(Failure::new("MCP tool schema changed; reviewed request withheld", false));
        }
        // Persist reservation before transmission. Canonical source and receipt
        // cannot be silently replaced while the operator reviews arguments.
        self.source_current(&binding.source_hash,&binding.source_model)?;
        self.journal.receipt.pending=Some(Pending { server_id:binding.server_id.clone(),tool_name:binding.tool_name.clone(),
            schema_sha256:binding.schema_hash.clone(),source_sha256:binding.source_hash.clone(),
            arguments_sha256:format!("{:x}",Sha256::digest(serde_json::to_vec(arguments).map_err(|_| Failure::new("MCP argument identity unavailable",false))?)) });
        if self.journal.write().is_err() { self.outcome_unknown.store(true,Ordering::Release);return Err(Failure::new("MCP durable pending receipt unavailable; request withheld",false)); }
        let client = self.clients.get_mut(&binding.server_id).unwrap();
        let result = client.call_tool(&binding.tool_name, arguments, cancel.clone()).await;
        let attempted = result.as_ref().map_or_else(|error| error.attempted, |_| true);
        if attempted { self.attempted_calls.fetch_add(1, Ordering::AcqRel); }
        if attempted && (result.is_err() || cancel.load(Ordering::Acquire)) {
            self.uncertain = true; self.outcome_unknown.store(true, Ordering::Release);
        }
        self.journal.receipt.calls_attempted=self.attempted_calls.load(Ordering::Acquire);
        self.journal.receipt.outcome_unknown=self.outcome_unknown.load(Ordering::Acquire);
        // Keep a known successful call pending until its scrubbed receipt is
        // accepted by the host; a cancelled/unsafe result cannot clear resume.
        if !attempted && !self.journal.receipt.outcome_unknown {self.journal.receipt.pending=None;}
        if self.journal.write().is_err() { self.outcome_unknown.store(true,Ordering::Release);return Err(Failure::new("MCP outcome receipt unavailable; automatic replay withheld",attempted)); }
        let result = result.map_err(Failure::from)?;
        let mut value = serde_json::to_value(result).map_err(|_| Failure::new("MCP result unavailable", true))?;
        // API tool continuations carry text/structured JSON only. Never fetch
        // linked resources or forward opaque image/audio/blob payloads.
        if let Some(content) = value["content"].as_array_mut() {
            for block in content {
                if block["type"] != "text" && !(block["type"] == "resource" && block["resource"]["text"].is_string()) {
                    *block = json!({"type":"text","text":"[Non-text MCP content withheld by DOXA]"});
                }
            }
        }
        if serde_json::to_vec(&value).map_err(|_| Failure::new("MCP result unavailable", true))?.len() > MAX_RESULT_BYTES {
            return Ok(json!({"is_error":true,"error":"MCP result exceeded host display/model limit; full content withheld","attempted":true}));
        }
        Ok(value)
    }
    async fn close(&mut self) {
        futures_util::future::join_all(self.clients.values_mut().map(Client::close)).await;
        self.clients.clear(); self.bindings.clear();
    }
}

#[derive(Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Pending {server_id:String,tool_name:String,schema_sha256:String,arguments_sha256:String,source_sha256:String}
#[derive(Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {version:u32,identity:Value,calls_attempted:u64,outcome_unknown:bool,pending:Option<Pending>}
struct Journal {path:PathBuf,receipt:Receipt,expected:Option<Receipt>}
impl Journal {
    fn new(cwd:&Path,id:&str,identity:Value,resume:bool)->io::Result<Self> {
        if !doxa_transcript::valid_session_id(id){return Err(io::Error::other("MCP session identity unavailable"));}
        let home=doxa_isolation::home()?;if !home.is_absolute() || home.starts_with(cwd){return Err(io::Error::other("MCP receipts must be outside workspace"));}
        let parent=home.join("mcp-receipts");let mut prefix=PathBuf::new();
        for part in parent.components(){if matches!(part,std::path::Component::ParentDir|std::path::Component::CurDir){return Err(io::Error::other("Unsafe MCP receipt parent"));}
            prefix.push(part);if let Ok(meta)=fs::symlink_metadata(&prefix){if !meta.is_dir() || meta.file_type().is_symlink(){return Err(io::Error::other("Unsafe MCP receipt parent"));}}}
        fs::DirBuilder::new().recursive(true).mode(0o700).create(&parent)?;
        let path=parent.join(format!("{id}.json"));let present=match fs::symlink_metadata(&path){Ok(_)=>true,Err(e) if e.kind()==io::ErrorKind::NotFound=>false,Err(e)=>return Err(e)};
        let expected=if present {Some(Self::read(&path)?)} else {None};
        if let Some(saved)=&expected {if !resume || saved.version!=1 || saved.identity!=identity {return Err(io::Error::other("MCP receipt resume identity mismatch"));}}
        let receipt=expected.clone().unwrap_or(Receipt{version:1,identity,calls_attempted:0,outcome_unknown:false,pending:None});
        if let Some(pending)=&receipt.pending {let hash=|v:&str|v.len()==64 && v.bytes().all(|ch|ch.is_ascii_hexdigit());
            if pending.server_id.len()>32 || pending.tool_name.len()>128 || !hash(&pending.schema_sha256) || !hash(&pending.arguments_sha256) || !hash(&pending.source_sha256){return Err(io::Error::other("Invalid MCP pending receipt"));}}
        let mut journal=Self{path,receipt,expected};if !present {journal.write()?;}Ok(journal)
    }
    fn read(path:&Path)->io::Result<Receipt> {serde_json::from_slice(&doxa_vendors::credentials::read_private_config(path,8192)?).map_err(io::Error::other)}
    fn current(&self)->io::Result<()> {match &self.expected {Some(expected) if &Self::read(&self.path)?==expected=>Ok(()),None if fs::symlink_metadata(&self.path).is_err_and(|e|e.kind()==io::ErrorKind::NotFound)=>Ok(()),_=>Err(io::Error::other("MCP receipt changed"))}}
    fn write(&mut self)->io::Result<()> {
        self.current()?;let parent=self.path.parent().unwrap();
        let directory=fs::OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(parent)?;
        let identity=directory.metadata()?;if identity.uid()!=unsafe{libc::geteuid()} || identity.mode()&0o077!=0{return Err(io::Error::other("MCP receipt parent must be private"));}
        let bytes=serde_json::to_vec(&self.receipt)?;if bytes.len()>8192{return Err(io::Error::other("MCP receipt bound"));}
        static NEXT:AtomicU64=AtomicU64::new(0);
        let temporary=std::ffi::CString::new(format!(".pending-{}-{}",std::process::id(),NEXT.fetch_add(1,Ordering::Relaxed))).unwrap();
        let target=std::ffi::CString::new(self.path.file_name().unwrap().as_encoded_bytes()).unwrap();
        let fd=unsafe{libc::openat(directory.as_raw_fd(),temporary.as_ptr(),libc::O_WRONLY|libc::O_CREAT|libc::O_EXCL|libc::O_NOFOLLOW|libc::O_CLOEXEC,0o600)};
        if fd<0{return Err(io::Error::last_os_error());}let mut file=unsafe{fs::File::from_raw_fd(fd)};
        let result=(||{file.write_all(&bytes)?;file.sync_all()?;self.current()?;
            let current=fs::OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW).open(parent)?.metadata()?;
            if (current.dev(),current.ino())!=(identity.dev(),identity.ino()){return Err(io::Error::other("MCP receipt directory changed"));}
            if unsafe{libc::renameat(directory.as_raw_fd(),temporary.as_ptr(),directory.as_raw_fd(),target.as_ptr())}!=0{return Err(io::Error::last_os_error());}directory.sync_all()}) (); 
        if result.is_err(){unsafe{libc::unlinkat(directory.as_raw_fd(),temporary.as_ptr(),0)};}
        result?;self.expected=Some(self.receipt.clone());Ok(())
    }
}

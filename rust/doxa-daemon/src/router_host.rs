//! Opt-in turn-boundary routing over one canonical VendorHost conversation.
use crate::vendor_host::VendorHost;
use doxa_router::{Billing, Candidate, Config, Ledger, Provider, Reason, RoutingInput};
use crate::router_cli::{CliOptions,Worker};
use doxa_runtime::{Host, PeerTurnOrigin};
use doxa_vendors::{TurnLimits, Vendor};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest,Sha256};
use std::time::Duration;
use std::{fs, io::{self, Read, Write}, path::{Path, PathBuf},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    sync::{atomic::{AtomicBool, Ordering}, Arc, Mutex}};

const JOURNAL_MAX: u64 = 256 * 1024;

fn vendor(provider: Provider) -> Vendor {
    match provider { Provider::Deepseek => Vendor::DeepSeek, Provider::Glm => Vendor::Glm,
        Provider::Claude | Provider::Codex => unreachable!("CLI targets do not use API transport") }
}
fn priced(input: u64, output: u64, candidate: &Candidate) -> Option<u64> {
    if candidate.billing != Billing::Api { return None; }
    let cost = (input as u128).checked_mul(candidate.input_usd_micros_per_million as u128)?
        .checked_add((output as u128).checked_mul(candidate.output_usd_micros_per_million as u128)?)?
        .div_ceil(1_000_000);
    u64::try_from(cost).ok()
}
fn worker_hold(candidate: &Candidate) -> Option<u64> {
    // Every paid continuation has the same enforced request-byte cap. Treat
    // each UTF-8 byte as a token for admission, including tool definitions and
    // tool results. Never reserve only the initial prompt's estimated usage.
    let input = candidate.context_tokens.checked_sub(candidate.max_output_tokens)?
        .checked_mul(doxa_vendors::MAX_TOOL_STEPS as u64 + 1)?;
    priced(input, candidate.max_output_tokens, candidate)
}
fn summary_hold(candidate: &Candidate) -> Option<u64> {
    priced(candidate.context_tokens.checked_sub(candidate.max_output_tokens)?, candidate.max_output_tokens, candidate)
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct WorkerLedger {
    input_tokens: u64,
    output_tokens: u64,
    estimated_actual_usd_micros: u64,
    retained_reservation_usd_micros: u64,
    accounting_unknown: bool,
    #[serde(default)]
    cli_turns: u64,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Allocation {
    child_session_id: String,
    parent_session_id: String,
    config_sha256: String,
    ceiling_usd_micros: u64,
    routing_calls: u64,
    routing_spend_usd_micros: u64,
    model: String,
    permission: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct State {
    version: u32,
    identity: Value,
    router: Ledger,
    worker: WorkerLedger,
    pinned: Option<String>,
    turn: u64,
    incomplete: bool,
    selection: Option<Value>,
    #[serde(default)]
    compaction: Option<Value>,
    #[serde(default)]
    pending_compaction: Option<Value>,
    #[serde(default)]
    pending_cli: Option<Value>,
    #[serde(default = "manual_permission")]
    permission: String,
    #[serde(default)]
    inherited: Option<Allocation>,
    #[serde(default)]
    allocations: Vec<Allocation>,
    #[serde(default)]
    config_path: Option<PathBuf>,
    #[serde(default)]
    config_model: Option<String>,
    receipts: Vec<Value>,
}
fn manual_permission() -> String { "default".into() }
fn canonical_value(value:Value)->Value {
    match value {
        Value::Object(rows)=>{
            let mut sorted=rows.into_iter().collect::<Vec<_>>();sorted.sort_by(|left,right|left.0.cmp(&right.0));
            Value::Object(sorted.into_iter().map(|(key,value)|(key,canonical_value(value))).collect())
        },
        Value::Array(rows)=>Value::Array(rows.into_iter().map(canonical_value).collect()),
        value=>value,
    }
}
impl State {
    fn held(&self) -> Option<u64> {
        self.router.reserved_usd_micros.max(self.router.actual_usd_micros)
            .checked_add(self.worker.estimated_actual_usd_micros)?
            .checked_add(self.worker.retained_reservation_usd_micros)
            .and_then(|held|self.allocations.iter().try_fold(held,|sum,row|sum.checked_add(row.ceiling_usd_micros)))
    }
    fn unknown(&self) -> bool {
        self.incomplete || self.pending_compaction.is_some() || self.pending_cli.is_some() || self.router.accounting_unknown || self.worker.accounting_unknown
    }
    fn estimated_spent(&self) -> Option<u64> {
        if self.unknown() { return None; }
        self.router.actual_usd_micros.checked_add(self.worker.estimated_actual_usd_micros)
    }
}

struct Journal { path: PathBuf }
impl Journal {
    fn new(cwd: &Path, id: &str) -> io::Result<Self> {
        if !doxa_transcript::valid_session_id(id) { return Err(io::Error::other("Invalid router session ID")); }
        let home = std::env::var_os("DOXA_HOME").filter(|value| !value.is_empty()).map(PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".doxa")))
            .ok_or_else(|| io::Error::other("DOXA home is unavailable"))?;
        if !home.is_absolute() || home.starts_with(cwd) { return Err(io::Error::other("Router home must be absolute and outside the working checkout")); }
        let parent = home.join("router");
        let mut prefix = PathBuf::new();
        for component in parent.components() {
            if matches!(component, std::path::Component::ParentDir | std::path::Component::CurDir) {
                return Err(io::Error::other("Unsafe router journal directory"));
            }
            prefix.push(component);
            if let Ok(meta) = fs::symlink_metadata(&prefix) {
                if !meta.is_dir() || meta.file_type().is_symlink() { return Err(io::Error::other("Unsafe router journal directory")); }
            }
        }
        fs::DirBuilder::new().recursive(true).mode(0o700).create(&parent)?;
        let meta = fs::symlink_metadata(&parent)?;
        if meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 {
            return Err(io::Error::other("Router journal directory must be private and owned"));
        }
        Ok(Self { path: parent.join(format!("{id}.router.json")) })
    }
    fn read(&self) -> io::Result<State> {
        let mut file = fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(&self.path)?;
        let meta = file.metadata()?;
        if !meta.is_file() || meta.nlink() != 1 || meta.uid() != unsafe { libc::geteuid() }
            || meta.mode() & 0o077 != 0 || meta.len() > JOURNAL_MAX { return Err(io::Error::other("Unsafe router journal")); }
        let mut bytes = Vec::new(); Read::by_ref(&mut file).take(JOURNAL_MAX + 1).read_to_end(&mut bytes)?;
        if bytes.len() as u64 > JOURNAL_MAX { return Err(io::Error::other("Router journal exceeds bounds")); }
        serde_json::from_slice(&bytes).map_err(io::Error::other)
    }
    fn write(&self, state: &State) -> io::Result<()> {
        let parent = self.path.parent().expect("router journal parent");
        let directory = fs::OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW).open(parent)?;
        let meta = directory.metadata()?;
        if meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 {
            return Err(io::Error::other("Unsafe router journal directory"));
        }
        let bytes = serde_json::to_vec(state)?;
        if bytes.len() as u64 > JOURNAL_MAX { return Err(io::Error::other("Router journal exceeds bounds")); }
        let mut temp = tempfile::Builder::new().prefix(".router-").tempfile_in(parent)?;
        temp.as_file().set_permissions(fs::Permissions::from_mode(0o600))?;
        temp.write_all(&bytes)?; temp.as_file().sync_all()?;
        temp.persist(&self.path).map_err(|error| error.error)?;
        directory.sync_all()
    }
}

pub struct RouterHost {
    inner: VendorHost,
    cli: CliOptions,
    cwd: PathBuf,
    active_worker: Mutex<Option<Arc<Worker>>>,
    peer_handler: Mutex<Option<doxa_runtime::PeerToolHandler>>,
    session_handler: Mutex<Option<doxa_runtime::PeerToolHandler>>,
    config: Config,
    ceiling_micros: Option<u64>,
    journal: Journal,
    state: Mutex<State>,
    turn_lock: Mutex<()>,
    active: AtomicBool,
    cancel: Arc<AtomicBool>,
    closing: AtomicBool,
    #[cfg(feature = "local-test-server")]
    jev_endpoint: Option<String>,
}

impl RouterHost {
    fn cost_scope(&self) -> &'static str {
        if self.config.decision()==doxa_router::DecisionModel::Jev {"jev_and_direct_api_only"}else{"decision_and_direct_api_only"}
    }

    #[cfg(test)]
    pub fn new(config: Config, cwd: &Path, id: &str, resume: bool, ceiling: Option<f64>,
        #[cfg(feature = "local-test-server")] endpoint: Option<String>) -> Result<Self, String> {
        let ceiling=ceiling.map(|value|doxa_router::usd_micros(&value.to_string()).map_err(|_|"Invalid router session ceiling")).transpose()?;
        Self::new_with_cli(config,cwd,id,resume,ceiling,CliOptions::default(),None,
            #[cfg(feature = "local-test-server")] endpoint)
    }
    pub fn new_with_cli(config: Config, cwd: &Path, id: &str, resume: bool, ceiling_micros: Option<u64>, cli: CliOptions, parent: Option<&str>,
        #[cfg(feature = "local-test-server")] endpoint: Option<String>) -> Result<Self,String> {
        config.validate().map_err(|_| "Invalid router configuration")?;
        if config.candidates.iter().any(|candidate| candidate.id == "auto") {
            return Err("Router target ID auto is reserved for automatic selection".into());
        }
        if ceiling_micros==Some(0){return Err("Invalid router session ceiling".into());}
        let cwd_identity = doxa_isolation::context_cwd(cwd).map_err(|_| "Router worktree identity unavailable")?;
        let journal = Journal::new(cwd, id).map_err(|_| "Router journal directory unavailable")?;
        let identity = json!({"engine":"router","session_id":id,"cwd":cwd_identity,
            "config_sha256":config.hash().map_err(|_| "Router config identity unavailable")?,
            "ceiling_usd_micros":ceiling_micros,"accounting":"aggregate_upper_rates_v1"});
        let state = if resume {
            let state = journal.read().map_err(|_| "Router resume requires a safe durable journal")?;
            if state.version != 1 || state.identity != identity || state.held().is_none()
                || !matches!(state.permission.as_str(),"default"|"auto") || state.receipts.len() > 1024 || state.pinned.as_ref().is_some_and(|id| config.candidate(id).is_none()) {
                return Err("Router resume identity or accounting changed".into());
            }
            state
        } else {
            if fs::symlink_metadata(&journal.path).is_ok() { return Err("Router journal already exists; use explicit resume".into()); }
            let inherited = parent.map(|parent| Self::inheritance(cwd,id,parent,&config,ceiling_micros)).transpose()?;
            State { version: 1, identity, router: Ledger::default(), worker: WorkerLedger::default(),
                pinned: inherited.as_ref().and_then(|row|(row.model!="auto").then(||row.model.clone())), turn: 0, incomplete: false, selection: None, compaction: None, pending_compaction: None, pending_cli: None,
                permission: inherited.as_ref().map(|row|row.permission.clone()).unwrap_or_else(manual_permission), inherited, allocations:Vec::new(),config_path:None,config_model:None, receipts: Vec::new() }
        };
        Self::validate_allocations(&state,&config,ceiling_micros)?;
        if let Some(inherited) = &state.inherited {
            if parent.is_some_and(|parent| parent != inherited.parent_session_id)
                || Self::inheritance(cwd,id,&inherited.parent_session_id,&config,ceiling_micros)? != *inherited {
                return Err("Router inherited allowance or parent review changed".into());
            }
        } else if parent.is_some() { return Err("Router child lacks its reviewed durable allocation".into()); }
        let initial = config.candidate(&config.fallback_id).ok_or("Router fallback is missing")?;
        let (initial_vendor,initial_model,initial_effort) = if initial.provider.is_cli() {
            (Vendor::DeepSeek,"router-unselected".to_owned(),"none".to_owned())
        } else {(vendor(initial.provider),initial.model.clone(),initial.effort.clone())};
        let inner = VendorHost::new_router(initial_vendor,initial_model,initial_effort,cwd,id,resume,
            #[cfg(feature = "local-test-server")] endpoint.clone())?;
        // Configuration labels/descriptions cannot smuggle a known credential
        // into Jev payloads, public controls or exact persisted target IDs.
        // Scrub prose and identity strings separately. Numeric bounds and JSON
        // syntax are not prose; text scrubbers may classify them as private IDs.
        let texts = [&config.jev_model, &config.criteria_version, &config.fallback_id].into_iter()
            .chain(config.candidates.iter().flat_map(|candidate| [&candidate.id,
                &candidate.model, &candidate.effort, &candidate.description]));
        for text in texts {
            if inner.public_prompt(text)? != *text { return Err("Router configuration contains redacted data".into()); }
        }
        if !resume { inner.initialize_router_conversation()?; }
        journal.write(&state).map_err(|_| "Router initial accounting could not be persisted")?;
        Ok(Self { inner, cli, cwd:cwd.to_owned(), active_worker:Mutex::new(None),peer_handler:Mutex::new(None),session_handler:Mutex::new(None),config, ceiling_micros, journal, state: Mutex::new(state),
            turn_lock: Mutex::new(()), active: AtomicBool::new(false), cancel: Arc::new(AtomicBool::new(false)), closing: AtomicBool::new(false),
            #[cfg(feature = "local-test-server")] jev_endpoint: endpoint })
    }

    pub fn shutdown(&self) {
        self.closing.store(true, Ordering::Release); self.cancel.store(true, Ordering::Release);
        if let Some(worker)=self.active_worker.lock().unwrap().clone() { let _=worker.host().call("interrupt",&json!({})); }
        // Catalog and protected startup own provisional children before an
        // active worker exists. Cancellation wakes those owners; keep the
        // daemon alive until their bounded teardown has completed.
        let deadline = std::time::Instant::now() + Duration::from_secs(6);
        while self.active.load(Ordering::Acquire) && std::time::Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        self.inner.shutdown();
    }
    pub fn record_config_path(&self,path:&Path)->Result<(),String> {
        let config=Config::load_with_model(path,Some(&self.config.jev_model)).map_err(|_|"Reviewed Router configuration changed before publication")?;
        if config.hash().ok()!=self.config.hash().ok() {return Err("Reviewed Router configuration changed before publication".into());}
        let mut state=self.state.lock().unwrap();
        if state.config_path.is_none() {state.config_path=Some(path.to_owned());}
        if state.config_model.as_deref().is_some_and(|model|model!=self.config.jev_model) {return Err("Router recorded decision service changed".into());}
        state.config_model=Some(self.config.jev_model.clone());
        self.save(&mut state)
    }
    fn migration_checkpoint(&self)->Result<Value,String> {
        let Ok(_turn)=self.turn_lock.try_lock() else {return Err("Router migration requires an idle turn boundary".into());};
        let state=self.state.lock().unwrap();
        if state.unknown() || self.active.load(Ordering::Acquire) || self.closing.load(Ordering::Acquire) {return Err("Router migration requires complete verified accounting".into());}
        Self::validate_allocations(&state,&self.config,self.ceiling_micros)?;
        let parent_allowance=if let Some(inherited)=&state.inherited {
            let id=state.identity["session_id"].as_str().ok_or("Router migration session identity unavailable")?;
            let (current,proof)=Self::inheritance_checkpoint(&self.cwd,id,&inherited.parent_session_id,&self.config,self.ceiling_micros)?;
            if current!=*inherited {
                return Err("Router migration inherited allowance or parent review changed".into());
            }
            Some(proof)
        }else{None};
        self.inner.routing_view("",self.config.max_input_bytes/2)?;
        let (bytes,proof)=doxa_engines::compact_hook::safe_read(&self.journal.path,JOURNAL_MAX as usize).map_err(|_|"Router migration journal is unsafe")?;
        let saved:Value=serde_json::from_slice(&bytes).map_err(|_|"Router migration journal is invalid")?;
        if serde_json::to_value(&*state).map_err(|_|"Router migration state unavailable")? != saved {return Err("Router migration journal changed externally".into());}
        let mut checkpoint=json!({"path":self.journal.path,"bytes":bytes.len(),"sha256":proof.json()["sha256"],
            "config_sha256":state.identity["config_sha256"],"ceiling_usd_micros":self.ceiling_micros,"session_id":state.identity["session_id"]});
        if let Some(parent)=parent_allowance {checkpoint["parent_allowance"]=parent;}
        Ok(checkpoint)
    }
    fn save(&self, state: &mut State) -> Result<(), String> {
        if self.journal.write(state).is_err() {
            state.worker.accounting_unknown = true; state.incomplete = true;
            return Err("Router accounting could not be persisted; further work withheld".into());
        }
        Ok(())
    }
    fn inheritance(cwd:&Path,id:&str,parent:&str,config:&Config,ceiling:Option<u64>) -> Result<Allocation,String> {
        Self::inheritance_checkpoint(cwd,id,parent,config,ceiling).map(|(allocation,_)|allocation)
    }
    fn inheritance_checkpoint(cwd:&Path,id:&str,parent:&str,config:&Config,ceiling:Option<u64>)->Result<(Allocation,Value),String> {
        let journal=Journal::new(cwd,parent).map_err(|_|"Router parent allowance journal is unavailable")?;
        let state=journal.read().map_err(|_|"Router parent allowance journal is unavailable")?;
        let hash=config.hash().map_err(|_|"Router config identity unavailable")?;
        if state.version!=1 || state.identity["engine"]!="router" || state.identity["session_id"]!=parent
            || state.identity["config_sha256"]!=hash || state.router.accounting_unknown || state.worker.accounting_unknown {
            return Err("Router parent configuration or allowance is unverified".into());
        }
        let row=state.allocations.iter().find(|row|row.child_session_id==id).cloned().ok_or("Router child needs a reviewed durable parent allocation")?;
        if row.parent_session_id!=parent || row.config_sha256!=hash || Some(row.ceiling_usd_micros)!=ceiling {
            return Err("Router child config or ceiling differs from its reviewed allocation".into());
        }
        Self::validate_allocations(&state,config,state.identity["ceiling_usd_micros"].as_u64())?;
        // Stable authority projection: ordinary parent turn/cost/receipt
        // counters may advance while this child's disjoint row stays exact.
        // The paired migration gate must still recheck full current bounds.
        let projection=canonical_value(json!({"version":state.version,"identity":state.identity,"allocation":row,
            "router_accounting_unknown":state.router.accounting_unknown,"worker_accounting_unknown":state.worker.accounting_unknown}));
        let bytes=serde_json::to_vec(&projection).map_err(|_|"Router parent allowance projection unavailable")?;
        Ok((row,json!({"path":journal.path,"parent_session_id":parent,"projection_sha256":format!("{:x}",Sha256::digest(bytes))})))
    }
    fn routing_limits(state:&State,config:&Config)->Option<(u64,u64)> {
        let (calls,spend)=state.inherited.as_ref().map(|row|(row.routing_calls,row.routing_spend_usd_micros))
            .unwrap_or((config.max_calls,config.max_spend_usd_micros));
        state.allocations.iter().try_fold((calls,spend),|(calls,spend),row|Some((calls.checked_sub(row.routing_calls)?,spend.checked_sub(row.routing_spend_usd_micros)?)))
    }
    fn validate_allocations(state:&State,config:&Config,ceiling:Option<u64>)->Result<(),String> {
        let hash=config.hash().map_err(|_|"Router config identity unavailable")?;
        let mut ids=std::collections::BTreeSet::new();
        if state.allocations.len()>64 || state.allocations.iter().any(|row| {
            !doxa_transcript::valid_session_id(&row.child_session_id) || !ids.insert(&row.child_session_id)
                || row.parent_session_id!=state.identity["session_id"].as_str().unwrap_or("") || row.config_sha256!=hash
                || row.ceiling_usd_micros==0 || !matches!(row.permission.as_str(),"default"|"auto")
                || row.model!="auto" && config.candidate(&row.model).is_none()
        }) { return Err("Router child allowance identity is invalid".into()); }
        let (calls,spend)=Self::routing_limits(state,config).ok_or("Router delegated routing allowance overflow")?;
        if state.router.calls>calls || state.router.held_usd_micros()>spend
            || ceiling.is_some_and(|ceiling|state.held().is_none_or(|held|held>ceiling)) {
            return Err("Router delegated allowance exceeds its durable ceiling".into());
        }
        Ok(())
    }
    fn child_plan(&self, model:Option<&str>)->Result<Value,String> {
        let state=self.state.lock().unwrap();
        self.child_plan_locked(&state,model)
    }
    fn child_plan_locked(&self,state:&State,model:Option<&str>)->Result<Value,String> {
        if state.router.accounting_unknown || state.worker.accounting_unknown || state.allocations.len()>=64
            || self.cancel.load(Ordering::Acquire) || self.closing.load(Ordering::Acquire) {
            return Err("Router child admission requires verified accounting and an active parent".into());
        }
        if let Some(path)=&state.config_path {
            let current=Config::load_with_model(path,Some(&self.config.jev_model)).map_err(|_|"Reviewed Router config is unavailable; child admission withheld")?;
            if current.hash().ok()!=self.config.hash().ok(){return Err("Reviewed Router config changed; child admission withheld".into());}
        }
        let ceiling=self.ceiling_micros.ok_or("Router child spawning requires a finite reviewed session ceiling")?;
        let amount=ceiling.checked_sub(state.held().ok_or("Router aggregate allowance overflow")?).ok_or("Router allowance exhausted")? / 2;
        if amount==0 { return Err("Router allowance is exhausted; child launch withheld".into()); }
        let (calls,spend)=Self::routing_limits(state,&self.config).ok_or("Router routing allowance overflow")?;
        let calls=calls.checked_sub(state.router.calls).ok_or("Router routing allowance exhausted")? / 2;
        let spend=spend.checked_sub(state.router.held_usd_micros()).ok_or("Router routing spend exhausted")? / 2;
        let model=model.unwrap_or(state.pinned.as_deref().unwrap_or("auto"));
        if model!="auto" && self.config.candidate(model).is_none() { return Err("Router child model must be auto or an exact reviewed target ID".into()); }
        Ok(json!({"parent_session_id":state.identity["session_id"],"config_sha256":state.identity["config_sha256"],
            "ceiling_usd_micros":amount,"routing_calls":calls,"routing_spend_usd_micros":spend,
            "model":model,"permission":state.permission,"allocation_policy":"disjoint_half_remaining_v1"}))
    }
    fn admit_child(&self,params:&Value)->Result<Value,String> {
        let child=params["child_session_id"].as_str().filter(|id|doxa_transcript::valid_session_id(id)).ok_or("Invalid Router child identity")?;
        let mut state=self.state.lock().unwrap();
        let plan=self.child_plan_locked(&state,params["review"]["model"].as_str())?;
        if params["review"]!=plan || state.allocations.iter().any(|row|row.child_session_id==child) {return Err("Router child allocation changed after review; request a fresh spawn".into());}
        let allocation=Allocation {child_session_id:child.into(),parent_session_id:plan["parent_session_id"].as_str().unwrap().into(),
            config_sha256:plan["config_sha256"].as_str().unwrap().into(),ceiling_usd_micros:plan["ceiling_usd_micros"].as_u64().unwrap(),
            routing_calls:plan["routing_calls"].as_u64().unwrap(),routing_spend_usd_micros:plan["routing_spend_usd_micros"].as_u64().unwrap(),
            model:plan["model"].as_str().unwrap().into(),permission:plan["permission"].as_str().unwrap().into()};
        let mut next=state.clone();next.allocations.push(allocation.clone());
        Self::validate_allocations(&next,&self.config,self.ceiling_micros)?;
        *state=next;
        self.save(&mut state)?;
        Ok(json!({"allocation":allocation,"retained_on_startup_failure":true}))
    }
    fn eligible(&self, input: u64, tools: bool, assistant: bool, held: u64, pinned: Option<&str>, summary: bool) -> Vec<String> {
        let mut catalogs = Vec::new();
        for provider in [Provider::Deepseek, Provider::Glm] {
            if pinned.is_some_and(|id| !self.config.candidates.iter().any(|candidate|candidate.id == id && candidate.provider == provider)) { continue; }
            let selected = vendor(provider);
            if !doxa_vendors::credentials::resolve(selected).ok().flatten().is_some() { continue; }
            if self.cancel.load(Ordering::Acquire) || self.closing.load(Ordering::Acquire) { break; }
            let advertised = tokio::runtime::Builder::new_current_thread().enable_all().build().ok()
                .and_then(|runtime| runtime.block_on(async {
                    let fetch = async {
                        #[cfg(feature = "local-test-server")]
                        if let Some(endpoint) = &self.jev_endpoint {
                            return doxa_vendors::catalog_models_local(selected, endpoint).await;
                        }
                        doxa_vendors::catalog_models(selected).await
                    };
                    let cancelled = async {
                        while !self.cancel.load(Ordering::Acquire) && !self.closing.load(Ordering::Acquire) {
                            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
                        }
                    };
                    tokio::select! { rows = fetch => rows, _ = cancelled => None,
                        _ = tokio::time::sleep(std::time::Duration::from_secs(2)) => None }
                }));
            if let Some(rows) = advertised.filter(|rows| !rows.is_empty()) { catalogs.push((provider, rows)); }
        }
        let mut cli_catalogs=Vec::new();
        if !summary { for provider in [Provider::Claude,Provider::Codex] {
            if !self.config.candidates.iter().any(|candidate|candidate.provider == provider
                && pinned.is_none_or(|id|candidate.id == id)) { continue; }
            if self.cancel.load(Ordering::Acquire) || self.closing.load(Ordering::Acquire) { break; }
            if let Ok(rows)=self.cli.catalog(provider,&self.cwd,&self.cancel) { cli_catalogs.push((provider,rows)); }
        } }
        self.config.candidates.iter().filter(|candidate| {
            if pinned.is_some_and(|id| candidate.id != id) { return false; }
            if candidate.provider.is_cli() {
                if summary || candidate.billing != Billing::CliAccount || tools && !candidate.supports_tools
                    || candidate.context_tokens.checked_sub(candidate.max_output_tokens).is_none_or(|cap|cap < input) { return false; }
                return cli_catalogs.iter().find(|(provider,_)|*provider == candidate.provider)
                    .is_some_and(|(_,rows)|rows.iter().any(|row|crate::router_cli::advertised(candidate,row)));
            }
            let Some((_, catalog)) = catalogs.iter().find(|(provider, _)| *provider == candidate.provider) else { return false; };
            let selected = vendor(candidate.provider);
            let capability = catalog.iter().find(|row| row.id == candidate.model);
            if !capability.is_some_and(|row| row.efforts.contains(&candidate.effort)) { return false; }
            let facts = doxa_engines::model_registry::lookup(selected.engine_id(), &candidate.model);
            let Some((input_rate, output_rate, _, _)) = facts.budget_bound_pair() else { return false; };
            if (candidate.input_usd_micros_per_million as f64) < (input_rate * 1_000_000.0).ceil()
                || (candidate.output_usd_micros_per_million as f64) < (output_rate * 1_000_000.0).ceil()
                || facts.context_window.value.is_some_and(|window| candidate.context_tokens > window)
                || candidate.context_tokens.checked_sub(candidate.max_output_tokens).is_none_or(|cap| cap < input)
                || tools && !candidate.supports_tools
                || selected == Vendor::DeepSeek && candidate.effort != "none" && tools && assistant { return false; }
            let Some(reserve) = (if summary { summary_hold(candidate) } else { worker_hold(candidate) }) else { return false; };
            !self.ceiling_micros.is_some_and(|ceiling| held.checked_add(reserve).is_none_or(|sum| sum > ceiling))
        }).map(|candidate| candidate.id.clone()).collect()
    }

    fn compact(&self, automatic:bool, emit: &mut dyn FnMut(Value)) -> Result<(), String> {
        if self.inner.lore_enabled() != Some(true) || doxa_lore::review_disabled().unwrap_or(true) {
            return Err("LORE review is disabled; router managed compaction blocked".into());
        }
        let (held, target_id) = {
            let state = self.state.lock().unwrap();
            if state.unknown() { return Err("Router accounting is incomplete or unknown; compaction withheld without replay".into()); }
            if state.receipts.len() >= 1024 { return Err("Router receipt capacity reached; compaction withheld".into()); }
            let last = state.selection.as_ref().map(|selection|selection["target_id"].as_str()
                .filter(|id|self.config.candidate(id).is_some()).ok_or("Recorded compaction target is invalid")).transpose()?;
            (state.held().ok_or("Router aggregate allowance overflow")?,
                state.pinned.as_deref().or(last).unwrap_or(&self.config.fallback_id).to_owned())
        };
        let candidate = self.config.candidate(&target_id).ok_or("Configured compaction target is missing")?;
        if candidate.provider.is_cli() { return Err("Router managed summary requires an eligible reviewed API target; pin an API target before /compact".into()); }
        let reserve = summary_hold(candidate).ok_or("Summary reservation overflow")?;
        if self.ceiling_micros.is_some_and(|ceiling| held.checked_add(reserve).is_none_or(|sum|sum > ceiling)) {
            return Err("Aggregate summary reservation exceeds the session ceiling".into());
        }
        let input_bytes = candidate.context_tokens.checked_sub(candidate.max_output_tokens).ok_or("Invalid summary byte cap")?;
        let required = self.inner.compaction_input_bytes(vendor(candidate.provider), &candidate.model, &candidate.effort, candidate.max_output_tokens)?;
        if required > input_bytes { return Err("Managed compaction request exceeds the configured byte cap".into()); }
        if !self.eligible(required, false, false, held, Some(&candidate.id), true).contains(&candidate.id) {
            return Err("Compaction target fails credentials, catalog, effort, context cap, price or aggregate allowance".into());
        }
        if self.cancel.load(Ordering::Acquire) || self.closing.load(Ordering::Acquire) {
            return Err("Router compaction cancelled before admission".into());
        }
        let limits = TurnLimits { input_bytes: usize::try_from(input_bytes).map_err(|_|"Summary byte cap overflow")?,
            output_tokens: candidate.max_output_tokens };
        let target = json!({"target_id":candidate.id,"engine":vendor(candidate.provider).engine_id(),
            "model":candidate.model,"effort":candidate.effort});
        self.inner.select_route(vendor(candidate.provider), &candidate.model, &candidate.effort, target.clone(), limits, None)?;
        let admitted = AtomicBool::new(false);
        let mut admit = |body: &Value, review: &Value| -> Result<(), ()> {
            if self.cancel.load(Ordering::Acquire) || self.closing.load(Ordering::Acquire)
                || body["model"] != candidate.model || body["max_tokens"] != candidate.max_output_tokens
                || body.get("tools").is_some() || serde_json::to_vec(body).map_err(|_|())?.len() > limits.input_bytes { return Err(()); }
            let mut state = self.state.lock().map_err(|_|())?;
            if state.unknown() || admitted.load(Ordering::Acquire)
                || self.ceiling_micros.is_some_and(|ceiling|state.held().and_then(|sum|sum.checked_add(reserve)).is_none_or(|sum|sum > ceiling)) { return Err(()); }
            state.worker.retained_reservation_usd_micros = state.worker.retained_reservation_usd_micros.checked_add(reserve).ok_or(())?;
            use sha2::Digest;
            state.pending_compaction = Some(json!({"operation":"compact","automatic":automatic,"summary_target":target,
                "request_sha256":format!("{:x}",sha2::Sha256::digest(serde_json::to_vec(body).map_err(|_|())?)),
                "source":review["expected_source"],"conversation_engine":"router",
                "input_cap_bytes":limits.input_bytes,"output_cap_tokens":limits.output_tokens,
                "worker_reservation_usd_micros":reserve}));
            state.incomplete = true;
            self.save(&mut state).map_err(|_|())?;
            admitted.store(true, Ordering::Release);
            Ok(())
        };
        let mut completed = false;
        self.inner.compact_routed(limits, &target, &mut admit, &mut |mut event| {
            if self.cancel.load(Ordering::Acquire) && event["type"] == "turn_started" {
                let _ = self.inner.call("interrupt", &json!({}));
            }
            let metadata = json!({"target_id":candidate.id,"engine":vendor(candidate.provider).engine_id(),"automatic":automatic,
                "model":candidate.model,"effort":candidate.effort,
                "reviewed":event["type"] == "compaction_done" || event["data"]["reviewed"] == true,
                "original_messages":event["data"]["original_messages"].as_u64().unwrap_or(0)});
            if matches!(event["type"].as_str(),Some("turn_started" | "compaction_done")) {
                event["data"]["compaction"] = metadata.clone();
            }
            if event["type"] == "turn_done" {
                let mut state = self.state.lock().unwrap();
                let pending = state.pending_compaction.clone();
                let attempted = event["data"]["summary_attempted"] == true;
                let usage = (attempted && event["data"]["usage_complete"] == true
                    && event["data"]["model_consistent"] == true && event["data"]["model"] == candidate.model)
                    .then(||event["data"]["prompt_tokens"].as_u64().zip(event["data"]["completion_tokens"].as_u64())).flatten();
                let mut cost = None;
                if !attempted {
                    if admitted.load(Ordering::Acquire) {
                        state.worker.retained_reservation_usd_micros -= reserve;
                        state.pending_compaction = None;
                        state.incomplete = false;
                    }
                    if !state.unknown() { cost = Some(0); }
                } else if admitted.load(Ordering::Acquire) {
                    if let Some((input,output,charge)) = usage.and_then(|(input,output)|priced(input,output,candidate)
                        .filter(|charge|*charge <= reserve).map(|charge|(input,output,charge))) {
                        if let Some((inputs,outputs,charges)) = state.worker.input_tokens.checked_add(input)
                            .zip(state.worker.output_tokens.checked_add(output))
                            .zip(state.worker.estimated_actual_usd_micros.checked_add(charge)).map(|((i,o),c)|(i,o,c)) {
                            state.worker.input_tokens = inputs; state.worker.output_tokens = outputs;
                            state.worker.estimated_actual_usd_micros = charges;
                            state.worker.retained_reservation_usd_micros -= reserve;
                            state.pending_compaction = None;
                            state.incomplete = false;
                            cost = Some(charge);
                        } else { state.worker.accounting_unknown = true; }
                    } else { state.worker.accounting_unknown = true; }
                } else { state.worker.accounting_unknown = true; state.incomplete = true; }
                state.compaction = Some(metadata.clone());
                let receipt = json!({"operation":"compact","automatic":automatic,"operation_id":state.receipts.len()+1,"turn":state.turn,
                    "summary_target":target,"summary_attempted":attempted,
                    "request_sha256":pending.as_ref().map(|pending|&pending["request_sha256"]),
                    "source":pending.as_ref().map(|pending|&pending["source"]),
                    "worker_usage":usage.map(|(input,output)|json!({"input_tokens":input,"output_tokens":output})),
                    "worker_reservation_usd_micros":if admitted.load(Ordering::Acquire){reserve}else{0},
                    "is_error":event["data"]["is_error"],"complete":!state.unknown()});
                state.receipts.push(receipt);
                let _ = self.save(&mut state);
                event["data"]["engine"] = json!(vendor(candidate.provider).engine_id());
                event["data"]["compaction"] = metadata;
                event["data"]["routing"] = state.selection.clone().unwrap_or(Value::Null);
                event["data"]["cost_usd"] = json!(if state.unknown(){None}else{cost.map(|sum|sum as f64/1_000_000.0)});
                event["data"]["session_cost_usd"] = json!(state.estimated_spent().map(|sum|sum as f64/1_000_000.0));
                event["data"]["cost_is_estimate"] = json!(true);
                event["data"]["cost_basis"] = json!("aggregate_upper_rates");
                event["data"]["cost_scope"] = json!(self.cost_scope());
                if self.config.candidates.iter().any(|row|row.provider.is_cli()) {
                    event["data"]["total_cost_usd"] = Value::Null;
                    event["data"]["cli_provider_charge_usd"] = Value::Null;
                }
                event["data"]["aggregate_held_usd"] = json!(state.held().map(|sum|sum as f64/1_000_000.0));
                event["data"]["accounting_unknown"] = json!(state.unknown());
                completed = true;
            }
            emit(event);
        });
        if !completed && admitted.load(Ordering::Acquire) {
            let mut state = self.state.lock().unwrap(); state.worker.accounting_unknown = true;
            self.save(&mut state)?;
        }
        Ok(())
    }

    fn run_cli(&self,text:&str,candidate:&Candidate,selection:&Value,outcome:&doxa_router::Outcome,
        peer_origin: Option<&PeerTurnOrigin>, emit:&mut dyn FnMut(Value)) -> Result<(),String> {
        let cap=candidate.context_tokens.checked_sub(candidate.max_output_tokens).ok_or("CLI context cap invalid")?;
        let prepared=self.inner.cli_context(text,cap);
        let (packet,source_hash)=match prepared {
            Ok(prepared)=>prepared,
            Err(error)=>{let mut state=self.state.lock().unwrap();state.incomplete=false;self.save(&mut state)?;return Err(error);}
        };
        let worker_id=crate::router_cli::worker_id().map_err(|_|"Router worker identity unavailable")?;
        let permission={
            let mut state=self.state.lock().unwrap();
            state.pending_cli=Some(json!({"operation":"cli_turn","worker_id":worker_id,
                "source_sha256":source_hash,"request_sha256":doxa_router::hash(&packet).map_err(|_|"CLI request identity unavailable")?,
                "target_id":candidate.id,"provider":candidate.provider,"model":candidate.model,"effort":candidate.effort,
                "billing":"cli_account","cost_scope":self.cost_scope(),"provider_charge_usd":null,
                "input_cap_bytes":cap,"output_cap_tokens":candidate.max_output_tokens,"permission":state.permission}));
            self.save(&mut state)?;state.permission.clone()
        };
        let worker=match self.cli.start(candidate,&self.cwd,&worker_id,&permission,self.inner.router_agent(),&self.cancel) {
            Ok(worker)=>worker,
            Err(_)=>{let mut state=self.state.lock().unwrap();state.pending_cli=None;state.incomplete=false;self.save(&mut state)?;
                return Err("Selected CLI worker authentication, catalog or protected startup unavailable; no user prompt submitted".into());}
        };
        if let Some(handler)=self.peer_handler.lock().unwrap().clone() { worker.host().set_peer_tool_handler(handler); }
        if let Some(handler)=self.session_handler.lock().unwrap().clone() { worker.host().set_session_tool_handler(handler); }
        // Revalidate canonical source after catalog/native initialization and
        // before submitting the only current user request. No old tool replay.
        if self.cancel.load(Ordering::Acquire) || self.closing.load(Ordering::Acquire)
            || self.inner.cli_context(text,cap).map(|(_,current)|current != source_hash).unwrap_or(true) {
            worker.shutdown();
            let mut state=self.state.lock().unwrap();state.pending_cli=None;state.incomplete=false;self.save(&mut state)?;
            return Err("Router CLI source changed or admission cancelled before submission".into());
        }
        *self.active_worker.lock().unwrap()=Some(worker.clone());
        struct WorkerAdmission<'a> { slot:&'a Mutex<Option<Arc<Worker>>>, worker:Arc<Worker> }
        impl Drop for WorkerAdmission<'_> {
            fn drop(&mut self) {
                if let Ok(mut slot)=self.slot.lock() {slot.take();}
                self.worker.shutdown();
            }
        }
        // Clear the outer control handle and stop the native worker even when
        // a provider callback panics. Shutdown itself is guarded once-only.
        let _admission=WorkerAdmission{slot:&self.active_worker,worker:worker.clone()};
        emit(json!({"type":"routing_selected","data":selection}));
        let mut output=String::new();let mut overflow=false;let mut terminal=None;
        worker.host().prompt(&packet,&mut |mut event| {
            match event["type"].as_str() {
                // Provider controls never replace the router Auto/pin picker.
                Some("model_changed"|"effort_changed"|"effort_verified")=>return,
                Some("turn_started")=>{
                    event["data"]["prompt"]=json!(text);
                    event["data"]["engine"]=json!(candidate.provider.engine_id());
                    event["data"]["routing"]=selection.clone();
                    if self.cancel.load(Ordering::Acquire) {let _=worker.host().call("interrupt",&json!({}));}
                }
                Some("text_delta") if event["data"]["parent_id"].is_null()=>{
                    if let Some(text)=event["data"]["text"].as_str() {
                        if output.len().saturating_add(text.len()) > candidate.max_output_tokens.saturating_mul(16).min(8*1024*1024) as usize {
                            overflow=true;let _=worker.host().call("interrupt",&json!({}));return;
                        }
                        output.push_str(text);
                    }
                }
                Some("billing")=>{
                    let native=event["data"].clone();
                    event["data"]=self.billing_snapshot().unwrap_or(Value::Null);
                    event["data"]["worker_billing"]=native;
                }
                Some("turn_done"|"turn_refused")=>{terminal=Some(event);return;}
                _=>{}
            }
            emit(event);
        });
        *self.active_worker.lock().unwrap()=None;
        worker.shutdown();
        let mut event=terminal.unwrap_or_else(||json!({"type":"turn_done","data":{"is_error":true}}));
        event["type"]=json!("turn_done");
        let data=&event["data"];
        let tokens=if candidate.provider == Provider::Claude {
            data["input_tokens"].as_u64().zip(data["cache_read_input_tokens"].as_u64())
                .zip(data["cache_creation_input_tokens"].as_u64())
                .and_then(|((fresh,read),create)|fresh.checked_add(read)?.checked_add(create))
                .zip(data["output_tokens"].as_u64())
        } else { data["input_tokens"].as_u64().zip(data["output_tokens"].as_u64()) };
        let usage=tokens.filter(|(input,output)|*input>0 && *output<=candidate.max_output_tokens
            && input.checked_add(candidate.max_output_tokens).is_some_and(|total|total<=candidate.context_tokens)
            && data["is_error"] == false && data["usage_complete"] == true
            && data["model_consistent"] == true && data["model"] == candidate.model && !overflow
            && !self.cancel.load(Ordering::Acquire) && !self.closing.load(Ordering::Acquire));
        let committed=usage.is_some() && self.inner.commit_cli_turn(text,&output,&source_hash,selection,peer_origin).is_ok();
        let mut state=self.state.lock().unwrap();
        let pending=state.pending_cli.clone();
        if committed {
            let (input,output)=usage.unwrap();
            if let Some((inputs,outputs,turns))=state.worker.input_tokens.checked_add(input)
                .zip(state.worker.output_tokens.checked_add(output)).zip(state.worker.cli_turns.checked_add(1)).map(|((i,o),t)|(i,o,t)) {
                state.worker.input_tokens=inputs;state.worker.output_tokens=outputs;state.worker.cli_turns=turns;
                state.pending_cli=None;state.incomplete=false;
            } else {state.worker.accounting_unknown=true;}
        } else {state.worker.accounting_unknown=true;}
        let complete=!state.unknown();
        let receipt=json!({"turn":state.turn,"selection":selection,"router":outcome,"worker":pending,
            "worker_usage":usage.map(|(input,output)|json!({"input_tokens":input,"output_tokens":output})),
            "worker_charge_usd":null,"complete":complete});
        state.receipts.push(receipt);
        let saved=self.save(&mut state).is_ok();
        if !complete || !saved { event["data"]["is_error"]=json!(true);
            event["data"]["error"]=json!("CLI worker usage, model, source or durable completion is unverified; further work withheld without replay"); }
        event["data"]["engine"]=json!(candidate.provider.engine_id());
        event["data"]["routing"]=selection.clone();
        event["data"]["prompt_tokens"]=json!(usage.map(|(input,_)|input));
        event["data"]["completion_tokens"]=json!(usage.map(|(_,output)|output));
        event["data"]["usage_complete"]=json!(usage.is_some());
        // Each private CLI identity executes one turn; provider session usage
        // is therefore the observed turn usage in the canonical conversation.
        event["data"]["usage_scope"]=json!("turn");
        event["data"]["worker_charge_usd"]=Value::Null;
        event["data"]["total_cost_usd"]=Value::Null;
        event["data"]["cost_usd"]=json!(if state.unknown(){None}else{outcome.router_cost_usd_micros.map(|sum|sum as f64/1_000_000.0)});
        event["data"]["session_cost_usd"]=json!(state.estimated_spent().map(|sum|sum as f64/1_000_000.0));
        event["data"]["cost_scope"]=json!(self.cost_scope());
        event["data"]["cost_basis"]=json!("aggregate_upper_rates");
        event["data"]["cost_is_estimate"]=json!(true);
        event["data"]["accounting_unknown"]=json!(state.unknown());
        event["data"]["aggregate_held_usd"]=json!(state.held().map(|sum|sum as f64/1_000_000.0));
        emit(event);Ok(())
    }

    fn route(&self, text: &str, peer_origin: Option<&PeerTurnOrigin>, emit: &mut dyn FnMut(Value)) -> Result<(), String> {
        if text.split_whitespace().next() == Some("/compact") {
            if text.trim() != "/compact" { return Err("Use /compact without arguments".into()); }
            return self.compact(false,emit);
        }
        if self.inner.resume_context_pending() {
            let target = {
                let state = self.state.lock().unwrap();
                if state.unknown() { return Err("Router accounting is incomplete or unknown; continuation withheld".into()); }
                state.pinned.clone().or_else(||state.selection.as_ref().and_then(|row|row["target_id"].as_str().map(str::to_owned)))
                    .unwrap_or_else(||self.config.fallback_id.clone())
            };
            let candidate = self.config.candidate(&target).ok_or("Saved continuation target is invalid")?;
            let cap = candidate.context_tokens.checked_sub(candidate.max_output_tokens).ok_or("Continuation input cap is invalid")?;
            let cap = self.config.auto_compact_percent.map_or(cap,|percent|cap.saturating_mul(percent as u64)/100);
            self.inner.prepare_router_resume(text,usize::try_from(cap).map_err(|_| "Continuation input cap overflow")?,emit)?;
        }
        let mut view = self.inner.routing_view(text, self.config.max_input_bytes / 2)?;
        if let Some(percent) = self.config.auto_compact_percent {
            let target = {
                let state = self.state.lock().unwrap();
                if state.unknown() { return Err("Router accounting is incomplete or unknown; automatic compaction withheld".into()); }
                state.pinned.clone().or_else(|| state.selection.as_ref().and_then(|row|row["target_id"].as_str().map(str::to_owned)))
                    .unwrap_or_else(||self.config.fallback_id.clone())
            };
            let candidate = self.config.candidate(&target).ok_or("Automatic compaction target is invalid")?;
            let cap = candidate.context_tokens.checked_sub(candidate.max_output_tokens).ok_or("Automatic compaction cap is invalid")?;
            let retained_input=self.inner.routing_view("",self.config.max_input_bytes/2)?.1;
            if view.3 && retained_input >= cap.saturating_mul(percent as u64) / 100 {
                // Summary terminals are progress events inside the original
                // turn. Never release the daemon busy gate before its worker.
                let mut succeeded = false;
                self.compact(true,&mut |mut event| {
                    event["data"]["automatic"] = json!(true);
                    if event["type"] == "turn_started" { event["type"] = json!("router_auto_compaction_started"); }
                    if event["type"] == "turn_done" {
                        succeeded = event["data"]["is_error"] == false && event["data"]["accounting_unknown"] == false;
                        event["type"] = json!("router_auto_compaction_finished");
                    }
                    emit(event);
                })?;
                if !succeeded { return Err("Automatic reviewed compaction did not complete; ordinary turn withheld".into()); }
                if self.cancel.load(Ordering::Acquire) || self.closing.load(Ordering::Acquire) {
                    return Err("Automatic compaction cancelled; ordinary turn withheld".into());
                }
                view = self.inner.routing_view(text, self.config.max_input_bytes / 2)?;
            }
        }
        let (summary, input_tokens, tools, assistant) = view;
        let (held, pinned) = {
            let state = self.state.lock().unwrap();
            if state.unknown() { return Err("Router accounting is incomplete or unknown; further turns withheld without replay".into()); }
            if state.receipts.len() >= 1024 { return Err("Router receipt capacity reached; further turns withheld".into()); }
            (state.held().ok_or("Router aggregate allowance overflow")?, state.pinned.clone())
        };
        let allowed = self.eligible(input_tokens, tools, assistant, held, pinned.as_deref(), false);
        if allowed.is_empty() { return Err("No configured target satisfies credentials, catalog, effort, tools, context cap and aggregate allowance".into()); }
        // A pinned target replaces the selection fallback only for this turn;
        // it does not alter the durable configuration identity or allowance.
        let mut turn_config = self.config.clone();
        if let Some(id) = &pinned { turn_config.fallback_id = id.clone(); }
        let required_output = allowed.iter().filter_map(|id| turn_config.candidate(id).map(|candidate| candidate.max_output_tokens)).min().unwrap();
        let prepared = doxa_router::prepare(&turn_config, &RoutingInput { summary, estimated_input_tokens: input_tokens,
            max_output_tokens: required_output, requires_tools: tools, allowed_candidate_ids: allowed })
            .map_err(|_| "Router input or configured fallback is ineligible")?;
        let key = turn_config.decision().credential().ok();
        let reservation = {
            let mut state = self.state.lock().unwrap();
            state.turn = state.turn.checked_add(1).ok_or("Router turn count overflow")?;
            let aggregate_ok = self.ceiling_micros.is_none_or(|ceiling| held.checked_add(prepared.reservation_usd_micros)
                .is_some_and(|sum| sum <= ceiling));
            let (calls,spend)=Self::routing_limits(&state,&self.config).ok_or("Router inherited routing allowance overflow")?;
            let decision_ok=state.router.calls<calls && state.router.held_usd_micros().checked_add(prepared.reservation_usd_micros).is_some_and(|sum|sum<=spend);
            let reservation = if pinned.is_none() && prepared.eligible_ids.len() > 1 && key.is_some() && aggregate_ok && decision_ok {
                doxa_router::reserve(&turn_config, &mut state.router, &prepared).ok()
            } else { None };
            state.incomplete = true;
            self.save(&mut state)?;
            reservation
        };
        let outcome = if reservation.is_some() {
            #[cfg(feature = "local-test-server")]
            { if let Some(endpoint) = &self.jev_endpoint {
                doxa_router::call_at(&turn_config, &prepared, key.as_deref().unwrap(), &self.cancel, endpoint)
            } else { doxa_router::call(&turn_config, &prepared, key.as_deref().unwrap(), &self.cancel) } }
            #[cfg(not(feature = "local-test-server"))]
            { doxa_router::call(&turn_config, &prepared, key.as_deref().unwrap(), &self.cancel) }
        } else { doxa_router::fallback(&turn_config, &prepared,
            if pinned.is_some() || prepared.eligible_ids.len() == 1 { Reason::SingleEligible }
            else if key.is_none() { Reason::Unavailable } else { Reason::Budget }) };
        {
            let mut state = self.state.lock().unwrap();
            if let Some(reservation) = &reservation {
                doxa_router::settle(&mut state.router, reservation, &outcome).map_err(|_| "Router settlement is incomplete")?;
            }
            self.save(&mut state)?;
            if self.cancel.load(Ordering::Acquire) || outcome.reason == Reason::Cancelled || self.closing.load(Ordering::Acquire) {
                // No worker was admitted. Verified settled Jev usage or a
                // no-HTTP outcome leaves a complete allowance after cancel.
                if !state.router.accounting_unknown { state.incomplete = false; self.save(&mut state)?; }
                return Err("Router turn cancelled; admitted reservations retained".into());
            }
            if state.router.accounting_unknown { return Err("Decision service usage is unknown; worker execution withheld".into()); }
        }
        let candidate = turn_config.candidate(&outcome.candidate_id)
            .filter(|candidate| prepared.eligible_ids.contains(&candidate.id)).ok_or("Router selected an ineligible target")?;
        let reserve = if candidate.provider.is_cli() {0} else {worker_hold(candidate).ok_or("Worker reservation overflow")?};
        let selection = {
            let mut state = self.state.lock().unwrap();
            if self.ceiling_micros.is_some_and(|ceiling| state.held().and_then(|sum| sum.checked_add(reserve)).is_none_or(|sum| sum > ceiling)) {
                state.incomplete = false; self.save(&mut state)?;
                return Err("Aggregate router and worker reservation exceeds the session ceiling".into());
            }
            let facts = doxa_engines::model_registry::lookup(candidate.provider.engine_id(), &candidate.model);
            let provider_window = match facts.context_window.provenance {
                doxa_engines::model_registry::Provenance::Static { source, as_of } =>
                    json!({"value":facts.context_window.value,"source":source,"read_on":as_of}),
                doxa_engines::model_registry::Provenance::Unknown =>
                    json!({"value":null,"source":null,"read_on":null}),
            };
            let selection = json!({"turn":state.turn,"target_id":candidate.id,"engine":candidate.provider.engine_id(),
                "model":candidate.model,"effort":candidate.effort,"billing":candidate.billing,
                "cost_scope":self.cost_scope(),"cli_provider_charge_usd":null,"route_mode":if pinned.is_some(){"pinned"}else{"auto"},
                "fallback_reason":if outcome.reason == Reason::Selected { Value::Null } else { serde_json::to_value(&outcome.reason).unwrap() },
                "latency_ms":outcome.latency_ms,"cost_usd":outcome.router_cost_usd_micros.map(|cost|cost as f64 / 1_000_000.0),
                "cost_is_estimate":true,"context_basis":"operator_request_byte_cap","provider_window":provider_window,
                "input_cap_bytes":candidate.context_tokens-candidate.max_output_tokens,"output_cap_tokens":candidate.max_output_tokens});
            state.worker.retained_reservation_usd_micros = state.worker.retained_reservation_usd_micros.checked_add(reserve).ok_or("Worker allowance overflow")?;
            state.selection = Some(selection.clone());
            self.save(&mut state)?;
            selection
        };
        if candidate.provider.is_cli() {
            return self.run_cli(text,candidate,&selection,&outcome,peer_origin,emit);
        }
        if let Err(error) = self.inner.select_route(vendor(candidate.provider), &candidate.model, &candidate.effort, selection.clone(),
            TurnLimits { input_bytes: usize::try_from(candidate.context_tokens-candidate.max_output_tokens).map_err(|_| "Worker context cap overflow")?,
                output_tokens: candidate.max_output_tokens }, Some(self.cancel.clone())) {
            let mut state = self.state.lock().unwrap();
            state.worker.retained_reservation_usd_micros -= reserve; state.incomplete = false; self.save(&mut state)?;
            return Err(error);
        }
        emit(json!({"type":"routing_selected","data":selection}));
        let mut completed = None;
        self.inner.prompt_with_peer_origin(text, peer_origin, &mut |mut event| {
            if self.cancel.load(Ordering::Acquire) && event["type"] == "turn_started" { let _ = self.inner.call("interrupt", &json!({})); }
            if event["type"] == "turn_done" {
                let mut state = self.state.lock().unwrap();
                let data = &event["data"];
                let usage = (data["is_error"] == false && data["usage_complete"] == true && data["model_consistent"] == true
                    && data["model"] == candidate.model).then(|| data["prompt_tokens"].as_u64().zip(data["completion_tokens"].as_u64())).flatten();
                if let Some((input, output)) = usage {
                    if let Some(cost) = priced(input, output, candidate).filter(|cost| *cost <= reserve) {
                        if let Some((inputs, outputs, costs)) = state.worker.input_tokens.checked_add(input)
                            .zip(state.worker.output_tokens.checked_add(output))
                            .zip(state.worker.estimated_actual_usd_micros.checked_add(cost)).map(|((i,o),c)|(i,o,c)) {
                            state.worker.input_tokens = inputs; state.worker.output_tokens = outputs;
                            state.worker.estimated_actual_usd_micros = costs;
                            state.worker.retained_reservation_usd_micros -= reserve;
                            state.incomplete = false;
                        } else { state.worker.accounting_unknown = true; }
                    } else { state.worker.accounting_unknown = true; }
                } else { state.worker.accounting_unknown = true; }
                let receipt = json!({"turn":state.turn,"selection":state.selection,"router":outcome,
                    "worker_usage":usage.map(|(input,output)|json!({"input_tokens":input,"output_tokens":output})),
                    "worker_reservation_usd_micros":reserve,"failure_category":data["failure_category"],"complete":!state.unknown()});
                state.receipts.push(receipt);
                if self.save(&mut state).is_err() { event["data"]["accounting_unknown"] = json!(true); }
                event["data"]["engine"] = json!(vendor(candidate.provider).engine_id());
                event["data"]["routing"] = state.selection.clone().unwrap_or(Value::Null);
                event["data"]["router_usage"] = json!(&outcome.usage);
                let turn_cost = if state.unknown() { None } else { usage
                    .and_then(|(input,output)|priced(input,output,candidate))
                    .zip(outcome.router_cost_usd_micros).and_then(|(worker,router)|worker.checked_add(router)) };
                event["data"]["cost_usd"] = json!(turn_cost.map(|sum|sum as f64/1_000_000.0));
                event["data"]["session_cost_usd"] = json!(state.estimated_spent().map(|sum|sum as f64/1_000_000.0));
                event["data"]["cost_is_estimate"] = json!(true);
                event["data"]["cost_basis"] = json!("aggregate_upper_rates");
                event["data"]["cost_scope"] = json!(self.cost_scope());
                if self.config.candidates.iter().any(|row|row.provider.is_cli()) {
                    event["data"]["total_cost_usd"] = Value::Null;
                    event["data"]["cli_provider_charge_usd"] = Value::Null;
                }
                event["data"]["aggregate_held_usd"] = json!(state.held().map(|sum|sum as f64/1_000_000.0));
                event["data"]["accounting_unknown"] = json!(state.unknown());
                completed = Some(());
            }
            emit(event);
        });
        if completed.is_none() {
            let mut state = self.state.lock().unwrap(); state.worker.accounting_unknown = true; self.save(&mut state)?;
        }
        Ok(())
    }

    fn prompt_impl(&self, text: &str, peer_origin: Option<&PeerTurnOrigin>, emit: &mut dyn FnMut(Value)) {
        if peer_origin.is_some_and(|origin| origin.validate().is_err()) {
            emit(json!({"type":"turn_refused","data":{"reason":"routing","message":"Invalid host peer provenance"}}));
            return;
        }
        let Ok(_turn) = self.turn_lock.try_lock() else {
            emit(json!({"type":"turn_refused","data":{"reason":"routing","message":"Router turn already running"}})); return;
        };
        if self.closing.load(Ordering::Acquire) { emit(json!({"type":"turn_refused","data":{"reason":"routing","message":"Router session is stopping"}})); return; }
        self.cancel.store(false, Ordering::Release); self.active.store(true, Ordering::Release);
        struct Active<'a>(&'a AtomicBool);
        impl Drop for Active<'_> { fn drop(&mut self) { self.0.store(false, Ordering::Release); } }
        let _active = Active(&self.active);
        let mut emit_mcp = |mut event| { self.inner.decorate_mcp(&mut event); emit(event); };
        if let Err(message) = self.route(text, peer_origin, &mut emit_mcp) {
            emit_mcp(json!({"type":"turn_refused","data":{"reason":"routing","message":message}}));
        }
    }
}

impl Host for RouterHost {
    fn prompt_with_peer_origin(&self, text: &str, origin: Option<&PeerTurnOrigin>, emit: &mut dyn FnMut(Value)) {
        self.prompt_impl(text, origin, emit);
    }
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
        self.prompt_impl(text, None, emit);
    }
    fn call(&self, method: &str, params: &Value) -> Result<Value, String> {
        match method {
            "router_migration_checkpoint" => self.migration_checkpoint(),
            "router_child_plan" => self.child_plan(params["model"].as_str()),
            "router_child_admit" => self.admit_child(params),
            "interrupt" => { self.cancel.store(true, Ordering::Release);
                if let Some(worker)=self.active_worker.lock().unwrap().clone() {let _=worker.host().call(method,params);}
                self.inner.call(method, params) }
            "answer_needs_input" => {
                if let Some(worker)=self.active_worker.lock().unwrap().clone() {worker.host().call(method,params)}
                else {self.inner.call(method,params)}
            }
            "set_permission_mode" => {
                let Ok(_turn)=self.turn_lock.try_lock() else {return Err("Router permission changes require idle workers".into());};
                if !self.config.candidates.iter().any(|row|row.provider.is_cli()) {return Err("No configured CLI permission surface".into());}
                let mode=params["mode"].as_str().filter(|mode|matches!(*mode,"default"|"auto")).ok_or("Router permission mode must be default or auto")?;
                let mut state=self.state.lock().unwrap();if state.unknown(){return Err("Router accounting incomplete; permission withheld".into());}
                state.permission=mode.into();self.save(&mut state)?;Ok(json!({"mode":mode,"verified":true}))
            }
            "stop" => { self.shutdown(); Ok(json!({})) }
            "list_models" => Ok(json!({"models":std::iter::once("auto").chain(self.config.candidates.iter().map(|row|row.id.as_str())).collect::<Vec<_>>(),
                "note":"Configured routing targets; eligibility checked at each turn"})),
            "set_model" => {
                let Ok(_turn) = self.turn_lock.try_lock() else { return Err("Router selection requires an idle session".into()); };
                if self.closing.load(Ordering::Acquire) { return Err("Router session is stopping".into()); }
                let model = params["model"].as_str().ok_or("Routing target ID required")?;
                if model != "auto" && self.config.candidate(model).is_none() { return Err("Unknown configured routing target".into()); }
                let mut state = self.state.lock().unwrap();
                if state.unknown() { return Err("Router accounting is incomplete; settings withheld".into()); }
                state.pinned = (model != "auto").then(|| model.to_owned()); self.save(&mut state)?;
                Ok(json!({"model":model,"effort":null}))
            }
            "set_effort" => Err("Router effort is configured per target; changing it requires a reviewed router config".into()),
            "get_settings" => { let state = self.state.lock().unwrap(); Ok(json!({"model":state.pinned.as_deref().unwrap_or("auto"),"effort":null,"routing":state.selection,"permission_mode":state.permission,"cost_scope":self.cost_scope()})) }
            "checkpoint_for_migration" => self.migration_checkpoint().map(|checkpoint|json!({"checkpointed":true,"router_checkpoint":checkpoint})),
            _ => self.inner.call(method, params),
        }
    }
    fn has_active_work(&self) -> bool { self.active.load(Ordering::Acquire) || self.inner.has_active_work() }
    fn initial_model(&self) -> Option<String> { Some(self.state.lock().unwrap().pinned.clone().unwrap_or_else(||"auto".into())) }
    fn initial_effort(&self) -> Option<String> { None }
    fn initial_permission_mode(&self)->String {self.state.lock().unwrap().permission.clone()}
    fn can_set_permission_mode(&self)->bool {self.config.candidates.iter().any(|row|row.provider.is_cli())}
    fn permission_change_requires_idle(&self)->bool {true}
    fn can_set_model(&self) -> bool { true }
    fn model_change_requires_idle(&self) -> bool { true }
    fn set_session_tool_handler(&self, handler:doxa_runtime::PeerToolHandler)->bool {
        if self.active.load(Ordering::Acquire) {return false;}
        *self.session_handler.lock().unwrap()=Some(handler.clone());self.inner.set_session_tool_handler(handler)
    }
    fn set_peer_tool_handler(&self, handler:doxa_runtime::PeerToolHandler)->bool {
        if self.active.load(Ordering::Acquire) {return false;}
        *self.peer_handler.lock().unwrap()=Some(handler.clone());self.inner.set_peer_tool_handler(handler)
    }
    fn peer_tools_ready(&self)->bool { self.inner.peer_tools_ready() }
    fn lore_enabled(&self)->Option<bool> { self.inner.lore_enabled() }
    fn lore_status(&self)->Option<Value> { self.inner.lore_status() }
    fn lore_scrub_status(&self)->Option<&'static str> { self.inner.lore_scrub_status() }
    fn public_prompt(&self,text:&str)->Result<String,String> { self.inner.public_prompt(text) }
    fn transcript_snapshot(&self)->io::Result<Option<(PathBuf,u64)>> { self.inner.transcript_snapshot() }
    fn billing_snapshot(&self)->Option<Value> {
        let state = self.state.lock().ok()?;
        Some(json!({"mode":if self.config.candidates.iter().any(|row|row.provider.is_cli()){"mixed"}else{"api"},
            "config_sha256":state.identity["config_sha256"],
            "cost_scope":self.cost_scope(),"cli_provider_charge_usd":null,
            "total_cost_usd":if !state.allocations.is_empty() || self.inner.external_mcp().is_some() || self.config.candidates.iter().any(|row|row.provider.is_cli()) {Value::Null}else{json!(state.estimated_spent().map(|sum|sum as f64/1_000_000.0))},
            "external_mcp":self.inner.external_mcp(),
            "routing":state.selection,"compaction":state.compaction,"router_usage":state.router,"worker_usage":state.worker,
            "cost_usd":state.estimated_spent().map(|sum|sum as f64/1_000_000.0),
            "session_cost_usd":state.estimated_spent().map(|sum|sum as f64/1_000_000.0),
            "cost_is_estimate":true,"cost_basis":"aggregate_upper_rates",
            "budget":{"ceiling_usd":self.ceiling_micros.map(|sum|sum as f64/1_000_000.0),
                "inherited":state.inherited,"delegated_allocations":state.allocations,
                "delegated_reservation_usd_micros":state.allocations.iter().map(|row|row.ceiling_usd_micros).sum::<u64>(),
                "aggregate_held_usd":state.held().map(|sum|sum as f64/1_000_000.0),
                "accounting_unknown":state.unknown(),"durable":true,"cost_scope":self.cost_scope(),"cost_basis":"aggregate_upper_rates"}}))
    }
}

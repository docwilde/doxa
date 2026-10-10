//! Opt-in turn-boundary routing over one canonical VendorHost conversation.
use crate::vendor_host::VendorHost;
use doxa_router::{Candidate, Config, Ledger, Provider, Reason, RoutingInput};
use doxa_runtime::Host;
use doxa_vendors::{TurnLimits, Vendor};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{fs, io::{self, Read, Write}, path::{Path, PathBuf},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    sync::{atomic::{AtomicBool, Ordering}, Mutex}};

const JOURNAL_MAX: u64 = 256 * 1024;

fn vendor(provider: Provider) -> Vendor {
    match provider { Provider::Deepseek => Vendor::DeepSeek, Provider::Glm => Vendor::Glm }
}
fn priced(input: u64, output: u64, candidate: &Candidate) -> Option<u64> {
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

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct WorkerLedger {
    input_tokens: u64,
    output_tokens: u64,
    estimated_actual_usd_micros: u64,
    retained_reservation_usd_micros: u64,
    accounting_unknown: bool,
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
    receipts: Vec<Value>,
}
impl State {
    fn held(&self) -> Option<u64> {
        self.router.reserved_usd_micros.max(self.router.actual_usd_micros)
            .checked_add(self.worker.estimated_actual_usd_micros)?
            .checked_add(self.worker.retained_reservation_usd_micros)
    }
    fn unknown(&self) -> bool {
        self.incomplete || self.router.accounting_unknown || self.worker.accounting_unknown
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
    config: Config,
    ceiling_micros: Option<u64>,
    journal: Journal,
    state: Mutex<State>,
    turn_lock: Mutex<()>,
    active: AtomicBool,
    cancel: AtomicBool,
    closing: AtomicBool,
    #[cfg(feature = "local-test-server")]
    jev_endpoint: Option<String>,
}

impl RouterHost {
    pub fn new(config: Config, cwd: &Path, id: &str, resume: bool, ceiling: Option<f64>,
        #[cfg(feature = "local-test-server")] endpoint: Option<String>) -> Result<Self, String> {
        config.validate().map_err(|_| "Invalid router configuration")?;
        if config.candidates.iter().any(|candidate| candidate.id == "auto") {
            return Err("Router target ID auto is reserved for automatic selection".into());
        }
        let ceiling_micros = ceiling.map(|ceiling| {
            if !ceiling.is_finite() || ceiling <= 0.0 || ceiling * 1_000_000.0 > u64::MAX as f64 {
                return Err("Invalid router session ceiling");
            }
            Ok((ceiling * 1_000_000.0).floor() as u64)
        }).transpose()?;
        let cwd_identity = doxa_isolation::context_cwd(cwd).map_err(|_| "Router worktree identity unavailable")?;
        let journal = Journal::new(cwd, id).map_err(|_| "Router journal directory unavailable")?;
        let identity = json!({"engine":"router","session_id":id,"cwd":cwd_identity,
            "config_sha256":config.hash().map_err(|_| "Router config identity unavailable")?,
            "ceiling_usd_micros":ceiling_micros,"accounting":"aggregate_upper_rates_v1"});
        let state = if resume {
            let state = journal.read().map_err(|_| "Router resume requires a safe durable journal")?;
            if state.version != 1 || state.identity != identity || state.held().is_none()
                || state.receipts.len() > 1024 || state.pinned.as_ref().is_some_and(|id| config.candidate(id).is_none()) {
                return Err("Router resume identity or accounting changed".into());
            }
            state
        } else {
            if fs::symlink_metadata(&journal.path).is_ok() { return Err("Router journal already exists; use explicit resume".into()); }
            State { version: 1, identity, router: Ledger::default(), worker: WorkerLedger::default(),
                pinned: None, turn: 0, incomplete: false, selection: None, receipts: Vec::new() }
        };
        let initial = config.candidate(&config.fallback_id).ok_or("Router fallback is missing")?;
        let inner = VendorHost::new_router(vendor(initial.provider), initial.model.clone(), initial.effort.clone(), cwd, id, resume,
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
        Ok(Self { inner, config, ceiling_micros, journal, state: Mutex::new(state),
            turn_lock: Mutex::new(()), active: AtomicBool::new(false), cancel: AtomicBool::new(false), closing: AtomicBool::new(false),
            #[cfg(feature = "local-test-server")] jev_endpoint: endpoint })
    }

    pub fn shutdown(&self) {
        self.closing.store(true, Ordering::Release); self.cancel.store(true, Ordering::Release);
        self.inner.shutdown();
    }
    fn save(&self, state: &mut State) -> Result<(), String> {
        if self.journal.write(state).is_err() {
            state.worker.accounting_unknown = true; state.incomplete = true;
            return Err("Router accounting could not be persisted; further work withheld".into());
        }
        Ok(())
    }
    fn eligible(&self, input: u64, tools: bool, assistant: bool, held: u64, pinned: Option<&str>) -> Vec<String> {
        let mut catalogs = Vec::new();
        for provider in [Provider::Deepseek, Provider::Glm] {
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
        self.config.candidates.iter().filter(|candidate| {
            if pinned.is_some_and(|id| candidate.id != id) { return false; }
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
            let Some(reserve) = worker_hold(candidate) else { return false; };
            !self.ceiling_micros.is_some_and(|ceiling| held.checked_add(reserve).is_none_or(|sum| sum > ceiling))
        }).map(|candidate| candidate.id.clone()).collect()
    }

    fn route(&self, text: &str, emit: &mut dyn FnMut(Value)) -> Result<(), String> {
        if text.split_whitespace().next() == Some("/compact") {
            return Err("Managed compaction is unavailable in the first router slice; routed review and compaction accounting are required".into());
        }
        let (summary, input_tokens, tools, assistant) = self.inner.routing_view(text, self.config.max_input_bytes / 2)?;
        let (held, pinned) = {
            let state = self.state.lock().unwrap();
            if state.unknown() { return Err("Router accounting is incomplete or unknown; further turns withheld without replay".into()); }
            if state.receipts.len() >= 1024 { return Err("Router receipt capacity reached; further turns withheld".into()); }
            (state.held().ok_or("Router aggregate allowance overflow")?, state.pinned.clone())
        };
        let allowed = self.eligible(input_tokens, tools, assistant, held, pinned.as_deref());
        if allowed.is_empty() { return Err("No configured target satisfies credentials, catalog, effort, tools, context cap and aggregate allowance".into()); }
        // A pinned target replaces the selection fallback only for this turn;
        // it does not alter the durable configuration identity or allowance.
        let mut turn_config = self.config.clone();
        if let Some(id) = &pinned { turn_config.fallback_id = id.clone(); }
        let required_output = allowed.iter().filter_map(|id| turn_config.candidate(id).map(|candidate| candidate.max_output_tokens)).min().unwrap();
        let prepared = doxa_router::prepare(&turn_config, &RoutingInput { summary, estimated_input_tokens: input_tokens,
            max_output_tokens: required_output, requires_tools: tools, allowed_candidate_ids: allowed })
            .map_err(|_| "Router input or configured fallback is ineligible")?;
        let key = doxa_router::credential().ok();
        let reservation = {
            let mut state = self.state.lock().unwrap();
            state.turn = state.turn.checked_add(1).ok_or("Router turn count overflow")?;
            let aggregate_ok = self.ceiling_micros.is_none_or(|ceiling| held.checked_add(prepared.reservation_usd_micros)
                .is_some_and(|sum| sum <= ceiling));
            let reservation = if pinned.is_none() && prepared.eligible_ids.len() > 1 && key.is_some() && aggregate_ok {
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
            if state.router.accounting_unknown { return Err("Jev usage is unknown; worker execution withheld".into()); }
        }
        let candidate = turn_config.candidate(&outcome.candidate_id)
            .filter(|candidate| prepared.eligible_ids.contains(&candidate.id)).ok_or("Router selected an ineligible target")?;
        let reserve = worker_hold(candidate).ok_or("Worker reservation overflow")?;
        let selection = {
            let mut state = self.state.lock().unwrap();
            if self.ceiling_micros.is_some_and(|ceiling| state.held().and_then(|sum| sum.checked_add(reserve)).is_none_or(|sum| sum > ceiling)) {
                state.incomplete = false; self.save(&mut state)?;
                return Err("Aggregate router and worker reservation exceeds the session ceiling".into());
            }
            let facts = doxa_engines::model_registry::lookup(vendor(candidate.provider).engine_id(), &candidate.model);
            let provider_window = match facts.context_window.provenance {
                doxa_engines::model_registry::Provenance::Static { source, as_of } =>
                    json!({"value":facts.context_window.value,"source":source,"read_on":as_of}),
                doxa_engines::model_registry::Provenance::Unknown =>
                    json!({"value":null,"source":null,"read_on":null}),
            };
            let selection = json!({"turn":state.turn,"target_id":candidate.id,"engine":vendor(candidate.provider).engine_id(),
                "model":candidate.model,"effort":candidate.effort,"route_mode":if pinned.is_some(){"pinned"}else{"auto"},
                "fallback_reason":if outcome.reason == Reason::Selected { Value::Null } else { serde_json::to_value(&outcome.reason).unwrap() },
                "latency_ms":outcome.latency_ms,"cost_usd":outcome.router_cost_usd_micros.map(|cost|cost as f64 / 1_000_000.0),
                "cost_is_estimate":true,"context_basis":"operator_request_byte_cap","provider_window":provider_window,
                "input_cap_bytes":candidate.context_tokens-candidate.max_output_tokens,"output_cap_tokens":candidate.max_output_tokens});
            state.worker.retained_reservation_usd_micros = state.worker.retained_reservation_usd_micros.checked_add(reserve).ok_or("Worker allowance overflow")?;
            state.selection = Some(selection.clone());
            self.save(&mut state)?;
            selection
        };
        if let Err(error) = self.inner.select_route(vendor(candidate.provider), &candidate.model, &candidate.effort, selection.clone(),
            TurnLimits { input_bytes: usize::try_from(candidate.context_tokens-candidate.max_output_tokens).map_err(|_| "Worker context cap overflow")?,
                output_tokens: candidate.max_output_tokens }) {
            let mut state = self.state.lock().unwrap();
            state.worker.retained_reservation_usd_micros -= reserve; state.incomplete = false; self.save(&mut state)?;
            return Err(error);
        }
        emit(json!({"type":"routing_selected","data":selection}));
        let mut completed = None;
        self.inner.prompt(text, &mut |mut event| {
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
                    "worker_reservation_usd_micros":reserve,"complete":!state.unknown()});
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
}

impl Host for RouterHost {
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
        let Ok(_turn) = self.turn_lock.try_lock() else {
            emit(json!({"type":"turn_refused","data":{"reason":"routing","message":"Router turn already running"}})); return;
        };
        if self.closing.load(Ordering::Acquire) { emit(json!({"type":"turn_refused","data":{"reason":"routing","message":"Router session is stopping"}})); return; }
        self.cancel.store(false, Ordering::Release); self.active.store(true, Ordering::Release);
        struct Active<'a>(&'a AtomicBool);
        impl Drop for Active<'_> { fn drop(&mut self) { self.0.store(false, Ordering::Release); } }
        let _active = Active(&self.active);
        if let Err(message) = self.route(text, emit) {
            emit(json!({"type":"turn_refused","data":{"reason":"routing","message":message}}));
        }
    }
    fn call(&self, method: &str, params: &Value) -> Result<Value, String> {
        match method {
            "interrupt" => { self.cancel.store(true, Ordering::Release); self.inner.call(method, params) }
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
            "get_settings" => { let state = self.state.lock().unwrap(); Ok(json!({"model":state.pinned.as_deref().unwrap_or("auto"),"effort":null,"routing":state.selection})) }
            "checkpoint_for_migration" => Err("Router handoffs are unavailable in the first slice".into()),
            _ => self.inner.call(method, params),
        }
    }
    fn has_active_work(&self) -> bool { self.active.load(Ordering::Acquire) || self.inner.has_active_work() }
    fn initial_model(&self) -> Option<String> { Some(self.state.lock().unwrap().pinned.clone().unwrap_or_else(||"auto".into())) }
    fn initial_effort(&self) -> Option<String> { None }
    fn can_set_model(&self) -> bool { true }
    fn model_change_requires_idle(&self) -> bool { true }
    fn set_session_tool_handler(&self, handler:doxa_runtime::PeerToolHandler)->bool {
        !self.active.load(Ordering::Acquire) && self.inner.set_session_tool_handler(handler)
    }
    fn set_peer_tool_handler(&self, handler:doxa_runtime::PeerToolHandler)->bool {
        !self.active.load(Ordering::Acquire) && self.inner.set_peer_tool_handler(handler)
    }
    fn peer_tools_ready(&self)->bool { self.inner.peer_tools_ready() }
    fn lore_enabled(&self)->Option<bool> { self.inner.lore_enabled() }
    fn lore_status(&self)->Option<Value> { self.inner.lore_status() }
    fn lore_scrub_status(&self)->Option<&'static str> { self.inner.lore_scrub_status() }
    fn public_prompt(&self,text:&str)->Result<String,String> { self.inner.public_prompt(text) }
    fn transcript_snapshot(&self)->io::Result<Option<(PathBuf,u64)>> { self.inner.transcript_snapshot() }
    fn billing_snapshot(&self)->Option<Value> {
        let state = self.state.lock().ok()?;
        Some(json!({"mode":"api","routing":state.selection,"router_usage":state.router,"worker_usage":state.worker,
            "cost_usd":state.estimated_spent().map(|sum|sum as f64/1_000_000.0),
            "session_cost_usd":state.estimated_spent().map(|sum|sum as f64/1_000_000.0),
            "cost_is_estimate":true,"cost_basis":"aggregate_upper_rates",
            "budget":{"ceiling_usd":self.ceiling_micros.map(|sum|sum as f64/1_000_000.0),
                "aggregate_held_usd":state.held().map(|sum|sum as f64/1_000_000.0),
                "accounting_unknown":state.unknown(),"durable":true,"cost_basis":"aggregate_upper_rates"}}))
    }
}

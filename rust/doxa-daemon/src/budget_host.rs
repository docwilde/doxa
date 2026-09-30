//! A session-start spend ceiling for hosts that report actual USD costs.
use doxa_runtime::Host;
use crate::peer_host::PEER_TURN_MARKER;
use serde_json::{json, Value};
use std::io::{self, Read, Write};
use std::fs;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

pub struct BudgetHost {
    inner: Arc<dyn Host>,
    ceiling: f64,
    pricing: Option<Pricing>,
    priced_model: Option<String>,
    state: Mutex<BudgetState>,
    journal: Option<Journal>,
}

/// Rates copied from the sourced Python 1.19 sheet (doxa/prices.py,
/// checked against provider pages 2026-09-30). Native vendor accounting deliberately charges every
/// prompt token at the fresh-input rate: cached tokens are a subset of the
/// prompt, and the published cached rate is lower for every row below.
#[derive(Clone, Copy)]
pub(super) struct Pricing { pub(super) input: f64, pub(super) output: f64, pub(super) source: &'static str }

pub(super) fn vendor_price(engine: &str, model: &str) -> Option<Pricing> {
    let (input, output) = match (engine, model) {
        ("codex", "gpt-6-astra") => (20.0, 100.0),
        ("codex", "gpt-5.6-sol") => (8.0, 40.0),
        ("codex", "gpt-5.6-terra") => (4.0, 24.0),
        ("codex", "gpt-5.6-luna") => (0.4, 2.4),
        ("codex", "gpt-5.5") => (12.5, 75.0),
        ("codex", "gpt-5.3-codex") => (3.5, 28.0),
        ("deepseek", "deepseek-flash") => (0.3, 1.2),
        ("deepseek", "deepseek-v4-pro") => (1.32, 3.96),
        ("glm", "glm-5.3-flash") => (0.15, 0.5),
        ("glm", "glm-5.3-flashx") => (0.37, 1.25),
        ("glm", "glm-5.3" | "glm-5.2" | "glm-5.1") => (1.4, 4.4),
        ("glm", "glm-5") => (1.0, 3.2),
        ("glm", "glm-4.7" | "glm-4.6" | "glm-4.5") => (0.6, 2.2),
        ("glm", "glm-4.7-flashx") => (0.07, 0.4),
        ("glm", "glm-4.5-air") => (0.2, 1.1),
        _ => return None,
    };
    let source = match engine {
        "codex" => "https://developers.openai.com/api/docs/pricing",
        "deepseek" => "https://api-docs.deepseek.com/quick_start/pricing",
        "glm" => "https://docs.z.ai/guides/overview/pricing",
        _ => return None,
    };
    Some(Pricing { input, output, source })
}

pub(super) fn priced_vendor_model(engine: &str, model: &str) -> bool {
    vendor_price(engine, model).is_some()
}

#[derive(Default)]
struct BudgetState {
    spent: f64,
    unknown: bool,
    input_total: u64,
    output_total: u64,
}

impl BudgetHost {
    pub fn new(inner: Arc<dyn Host>, ceiling: f64) -> Self {
        Self { inner, ceiling, pricing: None, priced_model: None, state: Mutex::new(BudgetState::default()), journal: None }
    }

    pub fn new_priced(inner: Arc<dyn Host>, ceiling: f64, engine: &str, model: &str) -> Result<Self, String> {
        let pricing = vendor_price(engine, model)
            .ok_or_else(|| format!("no native budget price for {engine}:{model}"))?;
        Ok(Self { inner, ceiling, pricing: Some(pricing), priced_model: Some(model.to_owned()), state: Mutex::new(BudgetState::default()), journal: None })
    }
}

/// Durable admission marker: a crash during a provider turn leaves accounting
/// unknown, so resume cannot grant a fresh allowance.
struct Journal { path: PathBuf, identity: Value }
impl Journal {
    fn write(&self, state: &BudgetState) -> io::Result<()> {
        let parent = self.path.parent().ok_or_else(|| io::Error::other("no budget directory"))?;
        let dir = fs::OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW).open(&parent)?;
        let meta = dir.metadata()?;
        if meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 {
            return Err(io::Error::other("budget directory must be private and owned"));
        }
        let mut temp = tempfile::Builder::new().prefix(".budget-").tempfile_in(parent)?;
        temp.as_file().set_permissions(fs::Permissions::from_mode(0o600))?;
        temp.write_all(&serde_json::to_vec(&json!({"version":1,"identity":self.identity,"spent":state.spent,"unknown":state.unknown,"input_total":state.input_total,"output_total":state.output_total}))?)?;
        temp.as_file().sync_all()?;
        temp.persist(&self.path).map_err(|error| error.error)?;
        dir.sync_all()
    }
}
impl BudgetHost {
    pub fn durable(mut self, path: PathBuf, mut identity: Value, resume: bool) -> io::Result<Self> {
        let parent = path.parent().ok_or_else(|| io::Error::other("no budget directory"))?.to_path_buf();
        fs::DirBuilder::new().recursive(true).mode(0o700).create(&parent)?;
        identity["accounting"] = match self.pricing {
            Some(price) => json!({"basis":"priced_conservative","model":self.priced_model,"input_rate":price.input,"output_rate":price.output,"source":price.source,"read_on":"2026-09-21"}),
            None => {
                // A billing-party reported dollar amount is model independent.
                // Model changes do not reset this session's spent allowance.
                identity["model"] = Value::Null;
                json!({"basis":"reported"})
            },
        };
        let journal = Journal { path, identity };
        // Validate the directory even before reading a potentially hostile file.
        let dir = fs::OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW).open(&parent)?;
        let meta = dir.metadata()?;
        if meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 { return Err(io::Error::other("budget directory must be private and owned")); }
        let mut state = BudgetState::default();
        if resume {
            let mut file = fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(&journal.path)?;
            let meta = file.metadata()?;
            if !meta.is_file() || meta.nlink() != 1 || meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 || meta.len() > 65536 { return Err(io::Error::other("untrusted budget journal")); }
            let mut bytes = Vec::new(); Read::by_ref(&mut file).take(65537).read_to_end(&mut bytes)?;
            let value: Value = serde_json::from_slice(&bytes)?;
            if value["version"] != 1 || value["identity"] != journal.identity { return Err(io::Error::other("budget resume identity changed")); }
            state.spent = value["spent"].as_f64().filter(|cost| cost.is_finite() && *cost >= 0.0).ok_or_else(|| io::Error::other("invalid durable spend"))?;
            state.input_total = value["input_total"].as_u64().ok_or_else(|| io::Error::other("invalid input accounting"))?;
            state.output_total = value["output_total"].as_u64().ok_or_else(|| io::Error::other("invalid output accounting"))?;
            state.unknown = value["unknown"].as_bool().ok_or_else(|| io::Error::other("invalid durable spend status"))?;
        } else if journal.path.try_exists()? { return Err(io::Error::other("budget journal already exists; explicit resume required")); }
        journal.write(&state)?;
        self.state = Mutex::new(state); self.journal = Some(journal); Ok(self)
    }
}

impl Host for BudgetHost {
    fn has_active_work(&self) -> bool { self.inner.has_active_work() }
    fn peer_tools_ready(&self) -> bool { self.inner.peer_tools_ready() }
    fn set_session_tool_handler(&self, handler: doxa_runtime::PeerToolHandler) -> bool { self.inner.set_session_tool_handler(handler) }
    fn set_peer_tool_handler(&self, handler: doxa_runtime::PeerToolHandler) -> bool { self.inner.set_peer_tool_handler(handler) }
    fn initial_effort(&self) -> Option<String> { self.inner.initial_effort() }
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
        // The runtime serializes prompt execution, but this mutex also keeps
        // observation and the next admission on one state boundary.
        let mut state = self.state.lock().unwrap_or_else(|poison| poison.into_inner());
        if state.unknown || state.spent >= self.ceiling {
            let peer_started = text.starts_with(PEER_TURN_MARKER);
            let peer_origin = if peer_started {
                text.lines().find(|line| line.starts_with("--- peer message "))
            } else { None };
            let message = if state.unknown {
                "Session spend is unknown; further turns are withheld until cost accounting is available"
            } else {
                "Session spend ceiling reached; further turns are withheld"
            };
            emit(json!({"type":"turn_refused","data":{
                "reason":"budget","message":message,"spent_usd":if state.unknown { None } else { Some(state.spent) },
                "ceiling_usd":self.ceiling,"peer_started":peer_started,"peer_origin":peer_origin,"prompt":null
            }}));
            return;
        }
        if let (Some(priced), Some(effective)) = (self.priced_model.as_deref(), self.inner.initial_model()) {
            if effective != priced {
                emit(json!({"type":"turn_refused","data":{"reason":"budget","message":"Effective model differs from the session price basis; prompt withheld","spent_usd":state.spent,"ceiling_usd":self.ceiling}}));
                return;
            }
        }
        if let Some(journal) = &self.journal {
            let pending = BudgetState { spent: state.spent, unknown: true, input_total: state.input_total, output_total: state.output_total };
            if journal.write(&pending).is_err() {
                state.unknown = true;
                emit(json!({"type":"turn_refused","data":{"reason":"budget","message":"Budget admission could not be persisted; prompt withheld"}}));
                return;
            }
        }
        // Also mark memory before entering provider code: runtime catches host
        // panics, and a caught panic must not reopen the allowance in process.
        state.unknown = true;
        let mut terminal_seen = false;
        self.inner.prompt(text, &mut |mut event| {
            if event["type"] == "turn_done" {
                terminal_seen = true;
                let cost = match self.pricing {
                    None => event["data"]["cost_usd"].as_f64(),
                    Some(price) => {
                        let data = &event["data"];
                        let model_ok = data["model"].as_str()
                            == self.priced_model.as_deref();
                        if data["usage_complete"] == true && data["model_consistent"] == true && model_ok {
                            let tokens = if data["usage_source"].as_str().is_some_and(|source| source.starts_with("codex_")) {
                                if data["turn_input_tokens"].as_u64().is_some() {
                                    data["turn_input_tokens"].as_u64().zip(data["turn_output_tokens"].as_u64())
                                } else {
                                    data["input_tokens"].as_u64().zip(data["output_tokens"].as_u64()).and_then(|(input, output)| {
                                        let delta = input.checked_sub(state.input_total).zip(output.checked_sub(state.output_total));
                                        state.input_total = input; state.output_total = output; delta
                                    })
                                }
                            } else { data["prompt_tokens"].as_u64().zip(data["completion_tokens"].as_u64()) };
                            tokens
                                // The inherited sourced row covers GPT-5.5
                                // prompts below 272K (doxa/prices.py priority row: <272K). Aggregate turn input is
                                // an upper bound for every call in that turn.
                                .filter(|(input, _)| self.priced_model.as_deref() != Some("gpt-5.5") || *input < 272_000)
                                .map(|(input, output)| (input as f64 * price.input + output as f64 * price.output) / 1_000_000.0)
                        } else { None }
                    }
                };
                match cost {
                    Some(cost) if cost.is_finite() && cost >= 0.0 => {
                        state.unknown = false;
                        state.spent += cost;
                        if !state.spent.is_finite() { state.unknown = true; }
                        if let Some(price) = self.pricing {
                            event["data"]["cost_usd"] = json!(cost);
                            event["data"]["session_cost_usd"] = json!(state.spent);
                            event["data"]["cost_basis"] = json!("priced_conservative");
                            event["data"]["cost_is_estimate"] = json!(true);
                            event["data"]["price_source"] = json!(price.source);
                            event["data"]["price_read_on"] = json!("2026-09-21");
                        }
                    }
                    _ => state.unknown = true,
                }
            }
            if event["type"] == "turn_done" {
                if let Some(journal) = &self.journal {
                    if journal.write(&state).is_err() { state.unknown = true; }
                }
            }
            emit(event);
        });
        if !terminal_seen { state.unknown = true; }
    }
    fn call(&self, method: &str, params: &Value) -> Result<Value, String> {
        if self.pricing.is_some() && method == "set_model" {
            return Err("model changes are unavailable under a priced session budget".into());
        }
        self.inner.call(method, params)
    }
    fn initial_model(&self) -> Option<String> { self.inner.initial_model() }
    fn initial_permission_mode(&self) -> String { self.inner.initial_permission_mode() }
    fn can_set_model(&self) -> bool { self.pricing.is_none() && self.inner.can_set_model() }
    fn can_set_permission_mode(&self) -> bool { self.inner.can_set_permission_mode() }
    fn account_snapshot(&self) -> Option<Value> { self.inner.account_snapshot() }
    fn billing_snapshot(&self) -> Option<Value> {
        // The prompt holds the accounting lock during provider execution;
        // hello/status must remain responsive so a person can answer asks.
        let state = self.state.try_lock().ok();
        let mut value = self.inner.billing_snapshot().unwrap_or_else(|| json!({}));
        value["budget"] = json!({"ceiling_usd":self.ceiling,
            "spent_usd":state.as_ref().filter(|state| !state.unknown).map(|state| state.spent),
            "accounting_unknown":state.as_ref().is_some_and(|state| state.unknown),
            "accounting_pending":state.is_none(),
            "cost_basis":if self.pricing.is_some() { "priced_conservative" } else { "reported" },
            "durable":self.journal.is_some()});
        Some(value)
    }
    fn lore_enabled(&self) -> Option<bool> { self.inner.lore_enabled() }
    fn lore_status(&self) -> Option<Value> { self.inner.lore_status() }
    fn lore_scrub_status(&self) -> Option<&'static str> { self.inner.lore_scrub_status() }
    fn public_prompt(&self, text: &str) -> Result<String, String> { self.inner.public_prompt(text) }
    fn transcript_snapshot(&self) -> io::Result<Option<(PathBuf, u64)>> { self.inner.transcript_snapshot() }
}

#[cfg(test)]
mod tests {
    use super::*;
    struct CostHost(Option<f64>);
    impl Host for CostHost {
        fn prompt(&self, _: &str, emit: &mut dyn FnMut(Value)) {
            emit(json!({"type":"turn_done","data":{"cost_usd":self.0}}));
        }
        fn call(&self, _: &str, _: &Value) -> Result<Value, String> { Ok(json!({})) }
    }
    #[test]
    fn measured_spend_refuses_the_next_turn_without_calling_host() {
        let host = BudgetHost::new(Arc::new(CostHost(Some(0.6))), 1.0);
        let mut events = Vec::new();
        for _ in 0..3 { host.prompt("hello", &mut |event| events.push(event)); }
        assert_eq!(events[2]["type"], "turn_refused");
        assert_eq!(events[2]["data"]["spent_usd"], 1.2);
        assert_eq!(events[2]["data"]["ceiling_usd"], 1.0);
    }
    #[test]
    fn missing_cost_fails_closed_after_one_turn() {
        let host = BudgetHost::new(Arc::new(CostHost(None)), 1.0);
        let mut events = Vec::new();
        for _ in 0..2 { host.prompt("hello", &mut |event| events.push(event)); }
        assert_eq!(events[1]["type"], "turn_refused");
        assert!(events[1]["data"]["spent_usd"].is_null());
    }
    struct VendorCostHost(Value);
    impl Host for VendorCostHost {
        fn prompt(&self, _: &str, emit: &mut dyn FnMut(Value)) {
            emit(json!({"type":"turn_done","data":self.0}));
        }
        fn call(&self, _: &str, _: &Value) -> Result<Value, String> { Ok(json!({})) }
        fn can_set_model(&self) -> bool { true }
    }
    fn priced_event() -> Value {
        json!({"model":"deepseek-flash","model_consistent":true,
            "usage_complete":true,"prompt_tokens":1_000_000,"completion_tokens":1_000_000})
    }
    #[test]
    fn vendor_price_conservatively_counts_all_prompt_tokens() {
        let host = BudgetHost::new_priced(
            Arc::new(VendorCostHost(priced_event())), 1.0, "deepseek", "deepseek-flash"
        ).unwrap();
        let mut events = Vec::new();
        host.prompt("hello", &mut |event| events.push(event));
        host.prompt("hello", &mut |event| events.push(event));
        assert_eq!(events[0]["data"]["cost_usd"], 1.5);
        assert_eq!(events[0]["data"]["cost_basis"], "priced_conservative");
        assert_eq!(events[0]["data"]["price_read_on"], "2026-09-21");
        assert_eq!(events[1]["type"], "turn_refused");
        assert!(host.call("set_model", &json!({"model":"glm-5-turbo"})).is_err());
        assert!(!host.can_set_model());
    }
    #[test]
    fn priced_vendor_requires_complete_usage_and_consistent_model() {
        for field in ["usage_complete", "model_consistent"] {
            let mut data = priced_event();
            data[field] = json!(false);
            let host = BudgetHost::new_priced(
                Arc::new(VendorCostHost(data)), 1.0, "deepseek", "deepseek-flash"
            ).unwrap();
            let mut events = Vec::new();
            host.prompt("hello", &mut |event| events.push(event));
            host.prompt("hello", &mut |event| events.push(event));
            assert_eq!(events[1]["type"], "turn_refused");
            assert!(events[1]["data"]["spent_usd"].is_null());
        }
        assert!(BudgetHost::new_priced(
            Arc::new(VendorCostHost(priced_event())), 1.0, "glm", "glm-5-turbo"
        ).is_err());
    }
    #[test]
    fn durable_resume_retains_spend_and_refuses_changed_identity() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("private/state.json");
        let identity = json!({"session":"same", "ceiling":1.0});
        let host = BudgetHost::new(Arc::new(CostHost(Some(0.6))), 1.0)
            .durable(path.clone(), identity.clone(), false).unwrap();
        host.prompt("hello", &mut |_| {});
        drop(host);
        let resumed = BudgetHost::new(Arc::new(CostHost(Some(0.6))), 1.0)
            .durable(path.clone(), identity.clone(), true).unwrap();
        let mut events = Vec::new();
        resumed.prompt("hello", &mut |event| events.push(event));
        resumed.prompt("hello", &mut |event| events.push(event));
        assert_eq!(events[1]["type"], "turn_refused");
        assert_eq!(events[1]["data"]["spent_usd"], 1.2);
        assert!(BudgetHost::new(Arc::new(CostHost(Some(0.6))), 1.0)
            .durable(path, json!({"session":"other"}), true).is_err());
    }
    #[test]
    fn interrupted_budget_marker_and_missing_journal_fail_closed() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("private/state.json");
        let identity = json!({"session":"same"});
        assert!(BudgetHost::new(Arc::new(CostHost(Some(0.6))), 1.0)
            .durable(path.clone(), identity.clone(), true).is_err());
        let host = BudgetHost::new(Arc::new(CostHost(None)), 1.0)
            .durable(path.clone(), identity.clone(), false).unwrap();
        host.prompt("hello", &mut |_| {});
        let resumed = BudgetHost::new(Arc::new(CostHost(Some(0.0))), 1.0)
            .durable(path, identity, true).unwrap();
        let mut events = Vec::new(); resumed.prompt("hello", &mut |event| events.push(event));
        assert_eq!(events[0]["type"], "turn_refused");
        assert!(events[0]["data"]["spent_usd"].is_null());
    }

    #[test]
    fn codex_price_requires_model_and_complete_usage_then_charges_turn_deltas() {
        let data = json!({"model":"gpt-5.6-sol","model_consistent":true,"usage_complete":true,
            "usage_source":"codex_cli_turn_completed","turn_input_tokens":1_000_000,"turn_output_tokens":1_000_000});
        let host = BudgetHost::new_priced(Arc::new(VendorCostHost(data.clone())), 47.0, "codex", "gpt-5.6-sol").unwrap();
        let mut events = Vec::new(); host.prompt("hello", &mut |event| events.push(event));
        host.prompt("hello", &mut |event| events.push(event));
        assert_eq!(events[0]["data"]["cost_usd"], 48.0);
        assert_eq!(events[1]["type"], "turn_refused");
        for field in ["model_consistent", "usage_complete"] {
            let mut broken = data.clone(); broken[field] = json!(false);
            let host = BudgetHost::new_priced(Arc::new(VendorCostHost(broken)), 100.0, "codex", "gpt-5.6-sol").unwrap();
            let mut events = Vec::new(); host.prompt("hello", &mut |event| events.push(event));
            host.prompt("hello", &mut |event| events.push(event));
            assert_eq!(events[1]["type"], "turn_refused");
        }
        assert!(!priced_vendor_model("codex", "unpriced"));
    }

    struct PanicHost;
    impl Host for PanicHost {
        fn prompt(&self, _: &str, _: &mut dyn FnMut(Value)) { panic!("simulated provider failure"); }
        fn call(&self, _: &str, _: &Value) -> Result<Value, String> { Ok(json!({})) }
    }
    #[test]
    fn caught_provider_panic_does_not_reopen_live_budget() {
        let host = BudgetHost::new(Arc::new(PanicHost), 1.0);
        assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| host.prompt("hello", &mut |_| {}))).is_err());
        let mut events = Vec::new(); host.prompt("hello", &mut |event| events.push(event));
        assert_eq!(events[0]["type"], "turn_refused");
        assert!(events[0]["data"]["spent_usd"].is_null());
    }

    #[test]
    fn codex_long_context_outside_published_row_fails_closed_at_boundary() {
        for (input, priced) in [(271_999, true), (272_000, false), (272_001, false)] {
            let data = json!({"model":"gpt-5.5","model_consistent":true,"usage_complete":true,
                "usage_source":"codex_cli_turn_completed","turn_input_tokens":input,"turn_output_tokens":0});
            let host = BudgetHost::new_priced(Arc::new(VendorCostHost(data)), 100.0, "codex", "gpt-5.5").unwrap();
            let mut events = Vec::new(); host.prompt("hello", &mut |event| events.push(event));
            host.prompt("hello", &mut |event| events.push(event));
            assert_eq!(events[0]["data"]["cost_usd"].is_number(), priced);
            assert_eq!(events[1]["type"] == "turn_refused", !priced);
        }
    }

    #[test]
    fn verified_initial_model_mismatch_is_refused_before_provider_admission() {
        struct InitialModelHost(std::sync::atomic::AtomicUsize);
        impl Host for InitialModelHost {
            fn initial_model(&self) -> Option<String> { Some("gpt-5.6-sol".into()) }
            fn prompt(&self, _: &str, _: &mut dyn FnMut(Value)) { self.0.fetch_add(1, std::sync::atomic::Ordering::Relaxed); }
            fn call(&self, _: &str, _: &Value) -> Result<Value, String> { Ok(json!({})) }
        }
        let inner = Arc::new(InitialModelHost(std::sync::atomic::AtomicUsize::new(0)));
        let host = BudgetHost::new_priced(inner.clone(), 1.0, "codex", "gpt-5.5").unwrap();
        let mut events = Vec::new(); host.prompt("must not infer", &mut |event| events.push(event));
        assert_eq!(inner.0.load(std::sync::atomic::Ordering::Relaxed), 0);
        assert_eq!(events[0]["type"], "turn_refused"); assert_eq!(events[0]["data"]["reason"], "budget");
        assert_eq!(events[0]["data"]["spent_usd"], 0.0);
    }

}

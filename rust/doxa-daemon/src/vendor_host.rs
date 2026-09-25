//! DeepSeek/GLM host with an explicitly enabled, read-only workspace tool.
use doxa_lore::LoreClient;
use doxa_runtime::Host;
use doxa_transcript::TranscriptStore;
use doxa_vendors::{Error, Vendor, MAX_TURN_DURATION};
use crate::vendor_tools::WorkspaceReadGate;
use serde_json::{json, Value};
use std::path::Path;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::watch;

pub struct VendorHost {
    vendor: Vendor,
    model: String,
    effort: String,
    lore: Mutex<LoreClient>,
    scrub_failed: AtomicBool,
    history: Mutex<Vec<Value>>,
    store: TranscriptStore,
    cwd: String,
    workspace_read: bool,
    storage_uncertain: AtomicBool,
    committed_bytes: AtomicU64,
    active: Mutex<Option<watch::Sender<bool>>>,
    turns: AtomicU64,
    closing: AtomicBool,
    balance: Arc<Mutex<Option<String>>>,
    balance_refreshing: Arc<AtomicBool>,
    #[cfg(feature = "local-test-server")]
    endpoint: Option<String>,
}

impl VendorHost {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        vendor: Vendor,
        model: String,
        effort: String,
        lore_python: &Path,
        cwd: &Path,
        session_id: &str,
        resume: bool,
        #[cfg(feature = "local-test-server")] endpoint: Option<String>,
    ) -> Result<Self, String> {
        if !std::env::var(vendor.env_var()).is_ok_and(|key| !key.is_empty()) {
            return Err(format!(
                "{} is required for native vendor chat",
                vendor.env_var()
            ));
        }
        let workspace_read = match std::env::var("DOXA_VENDOR_TOOLS") {
            Err(std::env::VarError::NotPresent) => false,
            Ok(value) if value.is_empty() => false,
            Ok(value) if value == "workspace-read" => true,
            _ => return Err("DOXA_VENDOR_TOOLS must be unset or workspace-read".to_owned()),
        };
        doxa_vendors::request_body(vendor, &model, &[], &effort)
            .map_err(|_| "invalid vendor effort".to_owned())?;
        let mut lore = LoreClient::spawn(lore_python, Duration::from_secs(5)).map_err(|_| {
            "LORE sidecar is unavailable; vendor session was not started".to_owned()
        })?;
        lore.scrub("DOXA scrub preflight").map_err(|_| {
            "LORE scrub preflight failed; vendor session was not started".to_owned()
        })?;
        if lore
            .scrub(&model)
            .map_err(|_| "LORE scrub failed for vendor model")?
            != model
        {
            return Err("vendor model cannot be stored without redaction".to_owned());
        }
        let cwd = cwd.to_string_lossy();
        let (projects_dir, slug) = lore.transcript_identity(&cwd).map_err(|_| {
            "LORE transcript identity unavailable; vendor session was not started".to_owned()
        })?;
        let store = TranscriptStore::new(&projects_dir, &slug, session_id)
            .map_err(|_| "vendor transcript directory unavailable".to_owned())?;
        let saved = store
            .read_vendor_messages(vendor.engine_id(), &model)
            .map_err(|_| "vendor messages state is unsafe or mismatched".to_owned())?;
        if resume && saved.is_none() {
            return Err("vendor resume requires saved messages state".to_owned());
        }
        if !resume && saved.is_some() {
            return Err("vendor session already has saved messages; use resume".to_owned());
        }
        if saved.is_none() && store.transcript_path().exists() {
            return Err("existing vendor transcript has no messages state".to_owned());
        }
        store
            .verify_vendor_transcript(vendor.engine_id(), saved.as_deref().unwrap_or(&[]))
            .map_err(|_| "vendor transcript and messages state diverged".to_owned())?;
        let committed_bytes = store
            .transcript_snapshot()
            .map_err(|_| "vendor transcript path is unsafe".to_owned())?
            .map_or(0, |(_, size)| size);
        let mut history = saved.unwrap_or_default();
        for message in &mut history {
            let content = message["content"].as_str().ok_or("invalid saved message")?;
            message["content"] = json!(lore
                .scrub(content)
                .map_err(|_| { "LORE scrub failed for saved vendor message" })?);
        }
        let host = Self {
            vendor,
            model,
            effort,
            lore: Mutex::new(lore),
            scrub_failed: AtomicBool::new(false),
            history: Mutex::new(history),
            store,
            cwd: cwd.into_owned(),
            workspace_read,
            storage_uncertain: AtomicBool::new(false),
            committed_bytes: AtomicU64::new(committed_bytes),
            active: Mutex::new(None),
            turns: AtomicU64::new(0),
            closing: AtomicBool::new(false),
            balance: Arc::new(Mutex::new(None)),
            balance_refreshing: Arc::new(AtomicBool::new(false)),
            #[cfg(feature = "local-test-server")]
            endpoint,
        };
        host.refresh_balance();
        Ok(host)
    }

    fn refresh_balance(&self) {
        if self.vendor != Vendor::DeepSeek { return; }
        #[cfg(feature = "local-test-server")]
        if self.endpoint.is_some() { return; }
        if self.balance_refreshing.swap(true, Ordering::AcqRel) { return; }
        let balance = Arc::clone(&self.balance);
        let refreshing = Arc::clone(&self.balance_refreshing);
        std::thread::spawn(move || {
            let latest = tokio::runtime::Builder::new_current_thread().enable_all().build()
                .ok().and_then(|runtime| runtime.block_on(doxa_vendors::deepseek_balance()));
            if let Ok(mut stored) = balance.lock() { *stored = latest; }
            refreshing.store(false, Ordering::Release);
        });
    }

    fn scrub(&self, text: &str) -> Result<String, ()> {
        let result = self.lore.lock().map_err(|_| ())
            .and_then(|mut lore| lore.scrub(text).map_err(|_| ()));
        if result.is_err() { self.scrub_failed.store(true, Ordering::Release); }
        result
    }

    pub fn shutdown(&self) {
        self.closing.store(true, Ordering::Release);
        self.cancel();
    }

    fn cancel(&self) {
        if let Ok(active) = self.active.lock() {
            if let Some(sender) = active.as_ref() {
                let _ = sender.send(true);
            }
        }
    }
}

impl Host for VendorHost {
    fn billing_snapshot(&self) -> Option<Value> {
        if self.vendor != Vendor::DeepSeek { return None; }
        self.balance.lock().ok().and_then(|value| value.as_ref()
            .map(|label| json!({"mode":"api","balance":label})))
    }
    fn lore_scrub_status(&self) -> Option<&'static str> {
        Some(if self.scrub_failed.load(Ordering::Acquire) { "unavailable" } else { "ready" })
    }
    fn public_prompt(&self, text: &str) -> Result<String, String> {
        self.scrub(text)
            .map_err(|_| "LORE scrub failed; prompt withheld".into())
    }

    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
        if self.storage_uncertain.load(Ordering::Acquire) {
            emit(done("Vendor storage state is uncertain; restart refused"));
            return;
        }
        if self.closing.load(Ordering::Acquire) {
            emit(done("Vendor session is stopping"));
            return;
        }
        let prompt = match self.scrub(text) {
            Ok(prompt) => prompt,
            Err(_) => {
                emit(done("LORE scrub failed; prompt withheld"));
                return;
            }
        };
        let (sender, cancel) = watch::channel(false);
        *self.active.lock().unwrap() = Some(sender);
        if self.closing.load(Ordering::Acquire) {
            self.cancel();
        }
        let started = Instant::now();
        emit(json!({"type":"turn_started","data":{"prompt":prompt,
            "vendor_tools":if self.workspace_read { "workspace-read" } else { "none" }}}));
        let mut history = self.history.lock().unwrap().clone();
        let saved_history = history.clone();
        let scrub_tool = |value: &str| self.scrub(value);
        let mut gate = WorkspaceReadGate::new(Path::new(&self.cwd), &scrub_tool);
        let gate = if self.workspace_read { Some(&mut gate as &mut dyn doxa_vendors::ToolGate) } else { None };
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build();
        let result = match runtime {
            Ok(runtime) => {
                #[cfg(feature = "local-test-server")]
                if let Some(endpoint) = self.endpoint.as_deref() {
                    runtime.block_on(doxa_vendors::run_turn_local(
                        self.vendor,
                        endpoint,
                        &self.model,
                        &self.effort,
                        &mut history,
                        &prompt,
                        gate,
                        cancel,
                        MAX_TURN_DURATION,
                        |_| {},
                    ))
                } else {
                    runtime.block_on(doxa_vendors::run_turn(
                        self.vendor,
                        &self.model,
                        &self.effort,
                        &mut history,
                        &prompt,
                        gate,
                        cancel,
                        MAX_TURN_DURATION,
                        |_| {},
                    ))
                }
                #[cfg(not(feature = "local-test-server"))]
                runtime.block_on(doxa_vendors::run_turn(
                    self.vendor,
                    &self.model,
                    &self.effort,
                    &mut history,
                    &prompt,
                    gate,
                    cancel,
                    MAX_TURN_DURATION,
                    |_| {},
                ))
            }
            Err(_) => Err(Error::Transport),
        };
        *self.active.lock().unwrap() = None;
        match result {
            Ok(outcome) => {
                // The crate masks its API key; LORE must scrub every other
                // secret before output is displayed or reused as history.
                // Tool messages are intentionally turn-local: the replay format
                // stores only paired user and final assistant messages.
                let final_text = history.last().and_then(|message| message["content"].as_str());
                let text = final_text.ok_or(()).and_then(|text| self.scrub(text));
                let reasoning = self.scrub(&outcome.reasoning);
                let model = self.scrub(outcome.model.as_deref().unwrap_or(&self.model));
                if let (Ok(text), Ok(reasoning), Ok(model)) = (text, reasoning, model) {
                    history = saved_history;
                    history.push(json!({"role":"user","content":prompt}));
                    history.push(json!({"role":"assistant","content":text}));
                    let timestamp = crate::iso_now();
                    if self
                        .store
                        .try_append_vendor_turn(
                            self.vendor.engine_id(),
                            &self.cwd,
                            &prompt,
                            &text,
                            &timestamp,
                            |value| {
                                self.scrub(value)
                                    .map_err(|_| std::io::Error::other("LORE scrub failed"))
                            },
                        )
                        .is_err()
                    {
                        self.storage_uncertain.store(true, Ordering::Release);
                        emit(done("Vendor transcript could not be safely saved"));
                        return;
                    }
                    let saved = self.store.try_write_vendor_messages(
                        self.vendor.engine_id(),
                        &self.model,
                        &history,
                        |value| {
                            self.scrub(value)
                                .map_err(|_| std::io::Error::other("LORE scrub failed"))
                        },
                    );
                    let Ok(history) = saved else {
                        self.storage_uncertain.store(true, Ordering::Release);
                        emit(done("Vendor history could not be safely saved"));
                        return;
                    };
                    let Ok(Some((_, committed_bytes))) = self.store.transcript_snapshot() else {
                        self.storage_uncertain.store(true, Ordering::Release);
                        emit(done("Vendor transcript commit boundary unavailable"));
                        return;
                    };
                    *self.history.lock().unwrap() = history;
                    self.committed_bytes
                        .store(committed_bytes, Ordering::Release);
                    if !reasoning.is_empty() {
                        emit(json!({"type":"reasoning_delta","data":{"text":reasoning}}));
                    }
                    if !text.is_empty() {
                        emit(json!({"type":"text_delta","data":{"text":text}}));
                    }
                    let turns = self.turns.fetch_add(1, Ordering::AcqRel) + 1;
                    self.refresh_balance();
                    emit(json!({"type":"turn_done","data":{"is_error":false,
                        "duration_ms":started.elapsed().as_millis() as u64,
                        "num_turns":turns,"model":model,
                        "prompt_tokens":outcome.usage.prompt_tokens,
                        "completion_tokens":outcome.usage.completion_tokens,
                        "usage_scope":"turn","usage_source":"vendor_response",
                        "cost_usd":null,"session_cost_usd":null,
                        "ctx_percentage":null,"ctx_tokens":null,"ctx_max_tokens":null}}));
                } else {
                    emit(done("LORE scrub failed; provider output withheld"));
                }
            }
            Err(error) => emit(done(match error {
                Error::Cancelled => "Vendor turn cancelled",
                Error::Timeout => "Vendor turn timed out",
                Error::MissingCredential(_) => "Vendor credential unavailable",
                Error::UnexpectedToolCall | Error::InvalidToolCall => {
                    "Vendor offered an unavailable tool"
                }
                _ => "Vendor turn failed",
            })),
        }
    }

    fn call(&self, method: &str, _: &Value) -> Result<Value, String> {
        match method {
            "interrupt" => {
                self.cancel();
                Ok(json!({}))
            }
            "stop" => {
                self.shutdown();
                Ok(json!({}))
            }
            _ => Err(format!("{method} is unavailable in the native vendor host")),
        }
    }

    fn transcript_snapshot(&self) -> std::io::Result<Option<(PathBuf, u64)>> {
        self.store.transcript_snapshot().map(|snapshot| {
            snapshot
                .map(|(path, size)| (path, size.min(self.committed_bytes.load(Ordering::Acquire))))
        })
    }
}

fn done(message: &str) -> Value {
    json!({"type":"turn_done","data":{"is_error":true,"error":message,
        "cost_usd":null,"session_cost_usd":null}})
}

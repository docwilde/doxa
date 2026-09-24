//! Plain-chat DeepSeek/GLM host. No model tools are advertised or executed.
use doxa_lore::LoreClient;
use doxa_runtime::Host;
use doxa_transcript::TranscriptStore;
use doxa_vendors::{Error, Vendor, MAX_TURN_DURATION};
use serde_json::{json, Value};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tokio::sync::watch;

pub struct VendorHost {
    vendor: Vendor,
    model: String,
    effort: String,
    lore: Mutex<LoreClient>,
    history: Mutex<Vec<Value>>,
    store: TranscriptStore,
    active: Mutex<Option<watch::Sender<bool>>>,
    turns: AtomicU64,
    closing: AtomicBool,
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
        let mut history = saved.unwrap_or_default();
        for message in &mut history {
            let content = message["content"].as_str().ok_or("invalid saved message")?;
            message["content"] = json!(lore
                .scrub(content)
                .map_err(|_| { "LORE scrub failed for saved vendor message" })?);
        }
        Ok(Self {
            vendor,
            model,
            effort,
            lore: Mutex::new(lore),
            history: Mutex::new(history),
            store,
            active: Mutex::new(None),
            turns: AtomicU64::new(0),
            closing: AtomicBool::new(false),
            #[cfg(feature = "local-test-server")]
            endpoint,
        })
    }

    fn scrub(&self, text: &str) -> Result<String, ()> {
        self.lore
            .lock()
            .map_err(|_| ())?
            .scrub(text)
            .map_err(|_| ())
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
    fn public_prompt(&self, text: &str) -> Result<String, String> {
        self.scrub(text)
            .map_err(|_| "LORE scrub failed; prompt withheld".into())
    }

    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
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
        emit(json!({"type":"turn_started","data":{"prompt":prompt}}));
        let mut history = self.history.lock().unwrap().clone();
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
                        None,
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
                        None,
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
                    None,
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
                let text = self.scrub(&outcome.text);
                let reasoning = self.scrub(&outcome.reasoning);
                let model = self.scrub(outcome.model.as_deref().unwrap_or(&self.model));
                if let (Ok(text), Ok(reasoning), Ok(model)) = (text, reasoning, model) {
                    if let Some(last) = history.last_mut() {
                        last["content"] = json!(text);
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
                        emit(done("Vendor history could not be safely saved"));
                        return;
                    };
                    *self.history.lock().unwrap() = history;
                    if !reasoning.is_empty() {
                        emit(json!({"type":"reasoning_delta","data":{"text":reasoning}}));
                    }
                    if !text.is_empty() {
                        emit(json!({"type":"text_delta","data":{"text":text}}));
                    }
                    let turns = self.turns.fetch_add(1, Ordering::AcqRel) + 1;
                    emit(json!({"type":"turn_done","data":{"is_error":false,
                        "duration_ms":started.elapsed().as_millis() as u64,
                        "num_turns":turns,"model":model,
                        "prompt_tokens":outcome.usage.prompt_tokens,
                        "completion_tokens":outcome.usage.completion_tokens,
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
}

fn done(message: &str) -> Value {
    json!({"type":"turn_done","data":{"is_error":true,"error":message,
        "cost_usd":null,"session_cost_usd":null}})
}

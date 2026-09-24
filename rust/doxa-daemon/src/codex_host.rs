use doxa_engines::codex_driver::{CodexCliDriver, DriverError, DriverOptions};
use doxa_lore::LoreClient;
use doxa_runtime::Host;
use serde_json::{json, Value};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tokio_util::sync::CancellationToken;

const SCRUB_FAILURE: &str = "[redacted: LORE scrub unavailable]";

/// A sequential Codex session. The daemon may call `stop` concurrently with
/// `prompt`, so the cancellation token lives outside the driver lock.
pub struct CodexHost {
    driver: Mutex<CodexCliDriver>,
    active: Mutex<Option<CancellationToken>>,
    scrub_failed: Arc<AtomicBool>,
    lore: Arc<Mutex<LoreClient>>,
    closing: AtomicBool,
}

impl CodexHost {
    pub fn new(options: DriverOptions, lore_python: &Path) -> Result<Self, String> {
        let mut client = LoreClient::spawn(lore_python, Duration::from_secs(5))
            .map_err(|_| "LORE sidecar is unavailable; Codex session was not started".to_owned())?;
        client.scrub("DOXA scrub preflight")
            .map_err(|_| "LORE scrub preflight failed; Codex session was not started".to_owned())?;
        let lore = Arc::new(Mutex::new(client));
        let scrub_failed = Arc::new(AtomicBool::new(false));
        let lore_for_scrub = lore.clone();
        let failure_for_scrub = scrub_failed.clone();
        let scrub = move |text: &str| -> String {
            match lore_for_scrub.lock().unwrap().scrub(text) {
                Ok(clean) => clean,
                Err(_) => {
                    failure_for_scrub.store(true, Ordering::Release);
                    SCRUB_FAILURE.to_owned()
                }
            }
        };
        Ok(Self {
            driver: Mutex::new(CodexCliDriver::new(options, scrub)),
            active: Mutex::new(None), scrub_failed, lore, closing: AtomicBool::new(false),
        })
    }

    fn cancel(&self) {
        if let Some(token) = self.active.lock().unwrap().as_ref() { token.cancel(); }
    }

    /// Give the driver time to reap its child process group before the daemon
    /// exits; process exit alone would leave a running Codex descendant.
    pub fn shutdown(&self) -> bool {
        self.closing.store(true, Ordering::Release);
        self.cancel();
        let deadline = Instant::now() + Duration::from_secs(5);
        while self.active.lock().unwrap().is_some() {
            if Instant::now() >= deadline { return false; }
            thread::sleep(Duration::from_millis(10));
        }
        true
    }
}

impl Host for CodexHost {
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
        if self.closing.load(Ordering::Acquire) {
            emit(json!({"type":"turn_done","data":{"is_error":true,"error":"Codex session is stopping"}}));
            return;
        }
        let token = CancellationToken::new();
        *self.active.lock().unwrap() = Some(token.clone());
        if self.closing.load(Ordering::Acquire) {
            *self.active.lock().unwrap() = None;
            emit(json!({"type":"turn_done","data":{"is_error":true,"error":"Codex session is stopping"}}));
            return;
        }
        let display_prompt = match self.public_prompt(text) {
            Ok(display) => display,
            Err(_) => {
                *self.active.lock().unwrap() = None;
                emit(json!({"type":"turn_done","data":{"is_error":true,"error":"LORE scrub failed; prompt withheld"}}));
                return;
            }
        };
        emit(json!({"type":"turn_started","data":{"prompt":display_prompt}}));
        let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build();
        let result = match runtime {
            Ok(runtime) => {
                let mut driver = self.driver.lock().unwrap();
                runtime.block_on(driver.run_turn(text, &token, |event| {
                    if self.scrub_failed.load(Ordering::Acquire) { token.cancel(); }
                    // The normalizer scrubbed all provider strings before
                    // this callback. Once scrubbing fails, discard output.
                    if !self.scrub_failed.load(Ordering::Acquire) {
                        emit(json!({"type":event.kind,"data":event.data}));
                    }
                }))
            }
            Err(error) => Err(DriverError::Spawn(error)),
        };
        *self.active.lock().unwrap() = None;
        if self.scrub_failed.load(Ordering::Acquire) {
            emit(json!({"type":"turn_done","data":{"is_error":true,"error":"LORE scrub failed; provider output withheld"}}));
            return;
        }
        match result {
            Ok(_) => {}, // The driver emitted a terminal event.
            Err(error) => {
                let reason = match error {
                    DriverError::Cancelled => "Codex turn cancelled",
                    DriverError::MissingResumeThread => "Codex thread ID unavailable; refusing to start a new thread",
                    DriverError::InvalidThreadId => "Codex thread ID is invalid",
                    DriverError::Spawn(_) => "Codex process could not start",
                };
                emit(json!({"type":"turn_done","data":{"is_error":true,"error":reason}}));
            }
        }
    }

    fn call(&self, method: &str, _: &Value) -> Result<Value, String> {
        match method {
            "interrupt" => { self.cancel(); Ok(json!({})) },
            "stop" => { self.closing.store(true, Ordering::Release); self.cancel(); Ok(json!({})) },
            _ => Err(format!("{method} is unavailable in the native Codex host")),
        }
    }

    fn public_prompt(&self, text: &str) -> Result<String, String> {
        if self.closing.load(Ordering::Acquire) || self.scrub_failed.load(Ordering::Acquire) {
            return Err("LORE scrub unavailable".to_owned());
        }
        self.lore.lock().unwrap().scrub(text).map_err(|_| {
            self.scrub_failed.store(true, Ordering::Release);
            "LORE scrub unavailable".to_owned()
        })
    }
}

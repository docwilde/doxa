use doxa_engines::antigravity::{self, Acp, Config};
use doxa_lore::{stream::StreamScrubber, LoreClient};
use doxa_runtime::{Host, PeerTurnOrigin};
use doxa_transcript::TranscriptStore;
use serde_json::{json, Value};
use std::{
    io,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, SyncSender},
        Mutex,
    },
    time::{Duration, Instant},
};

const ENGINE: &str = "antigravity";
const LORE_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_CONTEXT: usize = 64 * 1024;
const MAX_ANSWER: usize = 512 * 1024;
const MAX_REVIEW: usize = 12 * 1024;
const CANCEL_GRACE: Duration = Duration::from_secs(2);

struct Permission {
    id: String,
    rpc_id: Value,
    allow: String,
    deny: String,
    answer: Option<PermissionAnswer>,
}

struct PermissionAnswer {
    allow: bool,
    deadline: Instant,
    receipt: SyncSender<bool>,
}

struct Provider {
    acp: Option<Acp>,
    config: Config,
    inject_memory: bool,
}

pub struct AntigravityHost {
    provider: Mutex<Provider>,
    active: AtomicBool,
    cancel: AtomicBool,
    closing: AtomicBool,
    permission: Mutex<Option<Permission>>,
    cwd: String,
    id: String,
    session: String,
    store: TranscriptStore,
    lore: Mutex<LoreClient>,
    memory_enabled: bool,
    scrub_failed: AtomicBool,
    checkpoint_clean: AtomicBool,
}

impl AntigravityHost {
    pub fn new(
        executable: &Path,
        cwd: &Path,
        id: &str,
        resume: bool,
        model: Option<&str>,
        effort: Option<&str>,
    ) -> io::Result<Self> {
        let cwd_text = cwd
            .to_str()
            .ok_or_else(|| io::Error::other("Antigravity cwd must be UTF-8"))?;
        let mut lore = LoreClient::open(LORE_TIMEOUT)
            .map_err(|_| io::Error::other("LORE scrub service is required for Antigravity"))?;
        scrub(&mut lore, "DOXA scrub preflight")?;
        let (projects, slug) = lore.transcript_identity(cwd_text).map_err(|_| {
            io::Error::other("LORE transcript identity unavailable for Antigravity")
        })?;
        let store = TranscriptStore::new(&projects, &slug, id)?;
        let read = store.read_thread()?;
        let saved = match (resume, read) {
            (true, Some(saved))
                if saved["engine"] == ENGINE
                    && saved["session_id"] == id
                    && saved["cwd"] == cwd_text
                    && saved["turn_incomplete"] == false =>
            {
                store.verify_thread_checkpoint(&saved)?;
                Some(saved)
            }
            (true, _) => {
                return Err(io::Error::other(
                    "No complete Antigravity checkpoint matches this session and directory",
                ))
            }
            (false, None)
                if store.transcript_snapshot()?.is_none()
                    && !store.vendor_messages_path().exists() =>
            {
                None
            }
            (false, _) => {
                return Err(io::Error::other(
                    "Antigravity session ID already has local conversation state",
                ))
            }
        };
        let resume_id = saved
            .as_ref()
            .map(|state| {
                state["thread_id"]
                    .as_str()
                    .filter(|id| antigravity::valid_session_id(id))
                    .ok_or_else(|| io::Error::other("Invalid saved Antigravity session ID"))
            })
            .transpose()?;
        let cancel = AtomicBool::new(false);
        let mut acp = Acp::connect(executable, cwd, &cancel)?;
        let (session, mut config) = acp.session(cwd, resume_id, &cancel)?;
        let model = model.or_else(|| saved.as_ref().and_then(|state| state["model"].as_str()));
        let effort = effort.or_else(|| saved.as_ref().and_then(|state| state["effort"].as_str()));
        if let Some(model) = model {
            config.select(&mut acp, &session, false, model, &cancel)?;
        }
        if let Some(effort) = effort {
            config.select(&mut acp, &session, true, effort, &cancel)?;
        }
        let host = Self {
            provider: Mutex::new(Provider {
                acp: Some(acp),
                config,
                inject_memory: true,
            }),
            active: AtomicBool::new(false),
            cancel,
            closing: AtomicBool::new(false),
            permission: Mutex::new(None),
            cwd: cwd_text.to_owned(),
            id: id.to_owned(),
            session,
            store,
            lore: Mutex::new(lore),
            memory_enabled: doxa_state::lore_enabled_default(),
            scrub_failed: AtomicBool::new(false),
            checkpoint_clean: AtomicBool::new(resume),
        };
        {
            let provider = host.provider.lock().unwrap();
            host.check_identity(&provider.config)?;
        }
        Ok(host)
    }

    fn scrub(&self, text: &str) -> io::Result<String> {
        let result = scrub(&mut self.lore.lock().unwrap(), text);
        if result.is_err() {
            self.scrub_failed.store(true, Ordering::Release);
        }
        result
    }

    fn check_identity(&self, config: &Config) -> io::Result<()> {
        for value in [
            Some(self.session.as_str()),
            Some(self.cwd.as_str()),
            config.model(),
            config.effort(),
        ]
        .into_iter()
        .flatten()
        {
            if self.scrub(value)? != value {
                return Err(io::Error::other(
                    "Antigravity session metadata could not be retained safely",
                ));
            }
        }
        Ok(())
    }

    fn checkpoint(&self, config: &Config, incomplete: bool) -> io::Result<()> {
        if incomplete {
            self.checkpoint_clean.store(false, Ordering::Release);
        }
        self.check_identity(config)?;
        let fields = json!({
            "engine": ENGINE, "session_id": self.id, "cwd": self.cwd, "thread_id": self.session,
            "model": config.model(), "effort": config.effort(), "turn_incomplete": incomplete,
        });
        self.store
            .try_write_thread(fields.as_object().unwrap().clone(), |text| self.scrub(text))?;
        self.checkpoint_clean.store(!incomplete, Ordering::Release);
        Ok(())
    }

    fn memory(&self) -> Option<String> {
        if !self.memory_enabled {
            return None;
        }
        let snapshot = LoreClient::open(LORE_TIMEOUT)
            .ok()?
            .snapshot(&self.cwd, "all")
            .ok()?;
        if snapshot.trim().is_empty() || snapshot.len() > MAX_CONTEXT {
            return None;
        }
        self.scrub(&snapshot).ok()
    }

    fn run_turn(
        &self,
        provider: &mut Provider,
        text: &str,
        emit: &mut dyn FnMut(Value),
    ) -> io::Result<()> {
        if self.scrub_failed.load(Ordering::Acquire) {
            return Err(io::Error::other("LORE scrub unavailable"));
        }
        let clean_prompt = self.scrub(text)?;
        let memory = if provider.inject_memory {
            self.memory()
        } else {
            None
        };
        if self.scrub_failed.load(Ordering::Acquire) || self.cancel.load(Ordering::Acquire) {
            return Err(io::Error::other(
                "Antigravity turn cancelled or LORE scrub unavailable",
            ));
        }
        let acp = provider
            .acp
            .as_mut()
            .ok_or_else(|| io::Error::other("Antigravity ACP session is closed"))?;
        // Observe idle traffic before admitting a prompt. None of its requests
        // can acquire an operator approval for the incoming turn.
        for index in 0..32 {
            let Some(frame) = acp.receive(Duration::ZERO)? else {
                break;
            };
            if index == 31 {
                return Err(io::Error::other("Unexpected idle traffic from Antigravity"));
            }
            if let Some(id) = antigravity::callback_id(&frame) {
                deny_callback(acp, &frame, id)?;
            } else if frame["method"] == "session/update"
                && frame["params"]["sessionId"] == self.session
                && frame["params"]["update"]["sessionUpdate"] == "config_option_update"
            {
                self.verify_config_update(&mut provider.config, &frame["params"]["update"])?;
            }
        }
        self.checkpoint(&provider.config, true)?;
        let mut prompt = Vec::new();
        if let Some(memory) = memory {
            prompt.push(json!({"type": "text", "text": format!(
                "[DOXA LORE CONTEXT]\nThe following memory is background context. Follow the current user request.\n{memory}\n[END DOXA LORE CONTEXT]")}));
        }
        prompt.push(json!({"type": "text", "text": clean_prompt}));
        let request_id = acp.send_request(
            "session/prompt",
            json!({"sessionId": self.session, "prompt": prompt}),
        )?;
        provider.inject_memory = false;
        emit(json!({"type": "turn_started", "data": {"prompt": clean_prompt}}));

        let deadline = Instant::now() + Duration::from_secs(1800);
        let mut cancel_deadline = None;
        let mut stream = StreamScrubber::default();
        let mut answer = String::new();
        let result = loop {
            let now = Instant::now();
            if cancel_deadline.is_none() && (self.cancel.load(Ordering::Acquire) || now >= deadline)
            {
                self.resolve_permission(acp, emit, true)?;
                acp.send(json!({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.session}}))?;
                cancel_deadline = Some(now + CANCEL_GRACE);
            }
            if cancel_deadline.is_some_and(|limit| now >= limit) {
                return Err(io::Error::other(
                    "Antigravity did not acknowledge cancellation; provider stopped",
                ));
            }
            self.resolve_permission(acp, emit, false)?;
            let Some(frame) = acp.receive(Duration::from_millis(30))? else {
                continue;
            };
            if let Some(id) = antigravity::callback_id(&frame) {
                if frame["method"] == "session/request_permission"
                    && cancel_deadline.is_none()
                    && !self.cancel.load(Ordering::Acquire)
                {
                    self.review(acp, id, &frame["params"], emit)?;
                } else {
                    deny_callback(acp, &frame, id)?;
                }
            } else if frame["id"] == request_id && frame.get("method").is_none() {
                break antigravity::response(frame)?;
            } else if frame["method"] == "session/update"
                && frame["params"]["sessionId"] == self.session
                && cancel_deadline.is_none()
                && frame.get("id").is_none()
            {
                let update = &frame["params"]["update"];
                match update["sessionUpdate"].as_str() {
                    Some("agent_message_chunk") if update["content"]["type"] == "text" => {
                        let text = update["content"]["text"]
                            .as_str()
                            .ok_or_else(|| io::Error::other("Invalid Antigravity text chunk"))?;
                        let clean = stream.push(text, |chunk| self.scrub(chunk))?;
                        append_text(&mut answer, &clean, emit)?;
                    }
                    Some("tool_call" | "tool_call_update") => self.tool_update(update, emit)?,
                    Some("config_option_update") => {
                        self.verify_config_update(&mut provider.config, update)?;
                    }
                    _ => {}
                }
            }
        };
        self.resolve_permission(acp, emit, true)?;
        let reason = result["stopReason"].as_str();
        if !matches!(
            reason,
            Some("end_turn" | "max_tokens" | "max_turn_requests" | "refusal" | "cancelled")
        ) {
            return Err(io::Error::other(
                "Antigravity returned an unknown turn outcome",
            ));
        }
        // A cancelled response can end partway through a sensitive span.
        if reason != Some("cancelled") && cancel_deadline.is_none() {
            append_text(
                &mut answer,
                &stream.finish(|chunk| self.scrub(chunk))?,
                emit,
            )?;
        }
        self.store.try_append_vendor_turn(
            ENGINE,
            &self.cwd,
            &clean_prompt,
            &answer,
            &crate::iso_now(),
            |text| self.scrub(text),
        )?;
        self.checkpoint(&provider.config, false)?;
        emit(json!({"type": "turn_done", "data": {"stop_reason": reason,
            "cancelled": reason == Some("cancelled") || cancel_deadline.is_some()}}));
        Ok(())
    }

    fn verify_config_update(&self, current: &mut Config, update: &Value) -> io::Result<()> {
        let next = Config::parse(update)?;
        if current.model().is_some() && next.model() != current.model()
            || current.effort().is_some() && next.effort() != current.effort()
        {
            return Err(io::Error::other(
                "Antigravity changed the selected model or effort",
            ));
        }
        self.check_identity(&next)?;
        *current = next;
        Ok(())
    }

    fn review(
        &self,
        acp: &mut Acp,
        rpc_id: &Value,
        params: &Value,
        emit: &mut dyn FnMut(Value),
    ) -> io::Result<()> {
        let tool = &params["toolCall"];
        let id = tool["toolCallId"]
            .as_str()
            .filter(|id| !id.is_empty() && id.len() <= 128 && !id.chars().any(char::is_control));
        let options = params["options"]
            .as_array()
            .filter(|options| options.len() <= 16);
        let (Some(_), Some(options)) = (id, options) else {
            return permission_cancelled(acp, rpc_id);
        };
        if params["sessionId"] != self.session
            || (tool["rawInput"].is_null()
                && !tool["content"].as_array().is_some_and(|v| !v.is_empty()))
        {
            return permission_cancelled(acp, rpc_id);
        }
        let mut slot = self.permission.lock().unwrap();
        if slot.is_some() {
            // A repeated JSON-RPC ID could have changed the operation under review.
            if slot
                .as_ref()
                .is_some_and(|pending| &pending.rpc_id == rpc_id)
            {
                return Err(io::Error::other(
                    "Antigravity repeated a pending permission request ID",
                ));
            }
            return permission_cancelled(acp, rpc_id);
        }
        let option_id = |kind: &str| {
            let mut matching = options.iter().filter(|option| option["kind"] == kind);
            let value = matching.next()?["optionId"].as_str().filter(|id| {
                !id.is_empty() && id.len() <= 128 && !id.chars().any(char::is_control)
            })?;
            (matching.next().is_none()
                && options
                    .iter()
                    .filter(|option| option["optionId"] == value)
                    .count()
                    == 1)
                .then(|| value.to_owned())
        };
        let Some(allow) = option_id("allow_once") else {
            return permission_cancelled(acp, rpc_id);
        };
        let Some(deny) = option_id("reject_once") else {
            return permission_cancelled(acp, rpc_id);
        };
        let full = serde_json::to_string_pretty(tool)?;
        if full.len() > MAX_REVIEW
            || full.chars().any(|c| c.is_control() && c != '\n')
            || self.scrub(&full)? != full
        {
            return permission_cancelled(acp, rpc_id);
        }
        let name = tool["title"]
            .as_str()
            .filter(|s| s.len() <= 256 && !s.chars().any(char::is_control))
            .unwrap_or("Antigravity tool");
        let id = uuid::Uuid::new_v4().to_string();
        *slot = Some(Permission {
            id: id.clone(),
            rpc_id: rpc_id.clone(),
            allow,
            deny,
            answer: None,
        });
        drop(slot);
        emit(
            json!({"type": "needs_input", "data": {"id": id, "kind": "permission", "tool_name": name,
            "input_summary": full, "require_full_review": true}}),
        );
        Ok(())
    }

    fn resolve_permission(
        &self,
        acp: &mut Acp,
        emit: &mut dyn FnMut(Value),
        cancelled: bool,
    ) -> io::Result<()> {
        let mut slot = self.permission.lock().unwrap();
        if !slot
            .as_ref()
            .is_some_and(|pending| cancelled || pending.answer.is_some())
        {
            return Ok(());
        }
        let pending = slot.take().unwrap();
        let cancelled = cancelled
            || self.cancel.load(Ordering::Acquire)
            || pending
                .answer
                .as_ref()
                .is_some_and(|answer| Instant::now() >= answer.deadline);
        let option = pending.answer.as_ref().map(|answer| {
            if answer.allow {
                pending.allow
            } else {
                pending.deny
            }
        });
        let result = match option {
            Some(id) if !cancelled => acp.send(json!({"jsonrpc": "2.0", "id": pending.rpc_id,
                "result": {"outcome": {"outcome": "selected", "optionId": id}}})),
            _ => permission_cancelled(acp, &pending.rpc_id),
        };
        if let Some(answer) = pending.answer {
            let _ = answer.receipt.send(result.is_ok() && !cancelled);
        }
        emit(json!({"type": "needs_input_resolved", "data": {"id": pending.id}}));
        result
    }

    fn tool_update(&self, update: &Value, emit: &mut dyn FnMut(Value)) -> io::Result<()> {
        let Some(id) = update["toolCallId"]
            .as_str()
            .filter(|id| !id.is_empty() && id.len() <= 128 && !id.chars().any(char::is_control))
        else {
            return Ok(());
        };
        let id = self.scrub(id)?;
        let status = update["status"].as_str();
        let tool = update["kind"].as_str().filter(|kind| matches!(*kind,
            "read" | "edit" | "delete" | "move" | "search" | "execute" | "think" | "fetch" | "switch_mode"))
            .unwrap_or("tool");
        if matches!(status, Some("completed" | "failed")) {
            emit(json!({"type": "tool_result", "data": {"id": id,
                "result_summary": if status == Some("failed") { "Failed" } else { "Completed" },
                "is_error": status == Some("failed"),
                "evidence_tool": tool,
                "evidence_outcome": if status == Some("failed") { "failure" } else { "success" }}}));
        } else if update["sessionUpdate"] == "tool_call" {
            let name = update["title"]
                .as_str()
                .filter(|s| s.len() <= 256 && !s.chars().any(char::is_control))
                .unwrap_or("Antigravity tool");
            emit(
                json!({"type": "tool_call", "data": {"id": id, "name": self.scrub(name)?, "evidence_tool": tool, "input": {}}}),
            );
        }
        Ok(())
    }

    pub fn shutdown(&self) {
        self.closing.store(true, Ordering::Release);
        self.cancel.store(true, Ordering::Release);
        if let Some(mut acp) = self.provider.lock().unwrap().acp.take() {
            let _ = acp.close_session(&self.session, &AtomicBool::new(false));
        }
        if self.memory_enabled
            && self.checkpoint_clean.load(Ordering::Acquire)
            && !self.scrub_failed.load(Ordering::Acquire)
        {
            if let Ok(mut lore) = LoreClient::open(LORE_TIMEOUT) {
                let _ = lore.index_transcript(&self.cwd, &self.id);
            }
        }
    }
}

impl Host for AntigravityHost {
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
        let mut provider = self.provider.lock().unwrap();
        if self.closing.load(Ordering::Acquire) || provider.acp.is_none() {
            emit(
                json!({"type": "turn_refused", "data": {"message": "Antigravity session is closed"}}),
            );
            return;
        }
        self.cancel.store(false, Ordering::Release);
        self.active.store(true, Ordering::Release);
        if self.closing.load(Ordering::Acquire) {
            self.cancel.store(true, Ordering::Release);
        }
        let result = self.run_turn(&mut provider, text, emit);
        if result.is_err() {
            // A request can fail after reaching the agent. Never resend it.
            provider.acp.take();
            if let Some(pending) = self.permission.lock().unwrap().take() {
                emit(json!({"type": "needs_input_resolved", "data": {"id": pending.id}}));
            }
        }
        self.active.store(false, Ordering::Release);
        if let Err(error) = result {
            emit(
                json!({"type": "turn_done", "data": {"error": error.to_string(), "is_error": true}}),
            );
        }
    }

    fn prompt_with_peer_origin(
        &self,
        text: &str,
        origin: Option<&PeerTurnOrigin>,
        emit: &mut dyn FnMut(Value),
    ) {
        if origin.is_some() {
            emit(
                json!({"type": "turn_refused", "data": {"message": "Antigravity requires a standalone operator turn"}}),
            );
        } else {
            self.prompt(text, emit);
        }
    }

    fn call(&self, method: &str, params: &Value) -> Result<Value, String> {
        match method {
            "interrupt" => {
                if self.active.load(Ordering::Acquire) {
                    self.cancel.store(true, Ordering::Release);
                }
                Ok(json!({}))
            }
            "stop" => {
                self.closing.store(true, Ordering::Release);
                self.cancel.store(true, Ordering::Release);
                Ok(json!({}))
            }
            "answer_needs_input" => {
                let allow = match params["answer"]["decision"].as_str() {
                    Some("allow") => true,
                    Some("deny") => false,
                    _ => return Err("Antigravity requires a one-time Allow or Deny".into()),
                };
                let (tx, rx) = mpsc::sync_channel(1);
                {
                    let mut slot = self.permission.lock().unwrap();
                    let pending = slot
                        .as_mut()
                        .filter(|pending| {
                            params["id"] == pending.id
                                && pending.answer.is_none()
                                && !self.cancel.load(Ordering::Acquire)
                        })
                        .ok_or("Antigravity permission request expired or was answered")?;
                    pending.answer = Some(PermissionAnswer {
                        allow,
                        deadline: Instant::now() + Duration::from_secs(1),
                        receipt: tx,
                    });
                }
                Ok(json!({"applied": rx.recv_timeout(Duration::from_secs(3)).unwrap_or(false)}))
            }
            "list_models" => {
                let provider = self
                    .provider
                    .try_lock()
                    .map_err(|_| "Antigravity session is busy")?;
                let catalog = provider.config.catalog();
                if self
                    .scrub(&catalog.to_string())
                    .map_err(|e| e.to_string())?
                    != catalog.to_string()
                {
                    return Err("Antigravity catalog unavailable after scrubbing".into());
                }
                Ok(catalog)
            }
            "set_model" | "set_effort" => {
                let field = if method == "set_model" {
                    "model"
                } else {
                    "effort"
                };
                let requested = params[field]
                    .as_str()
                    .ok_or("Antigravity selection required")?;
                let mut provider = self
                    .provider
                    .try_lock()
                    .map_err(|_| "Antigravity session is busy")?;
                let Provider { acp, config, .. } = &mut *provider;
                let driver = acp.as_mut().ok_or("Antigravity ACP session is closed")?;
                let is_effort = field == "effort";
                let selection = config
                    .selection(is_effort, requested)
                    .map_err(|e| e.to_string())?;
                if is_effort && selection != requested {
                    return Err("Choose an exact Antigravity effort ID".into());
                }
                let result = config
                    .select(driver, &self.session, is_effort, &selection, &self.closing)
                    .and_then(|selected| {
                        self.check_identity(config)?;
                        if self.checkpoint_clean.load(Ordering::Acquire) {
                            self.checkpoint(config, false)?;
                        }
                        if is_effort {
                            Ok(json!({"effort": selected}))
                        } else {
                            Ok(json!({"model": selected, "effort": config.effort()}))
                        }
                    });
                if result.is_err() {
                    acp.take();
                }
                result.map_err(|e| e.to_string())
            }
            _ => Err(format!("{method} unavailable for Antigravity")),
        }
    }

    fn initial_model(&self) -> Option<String> {
        self.provider
            .lock()
            .unwrap()
            .config
            .model()
            .map(str::to_owned)
    }
    fn initial_effort(&self) -> Option<String> {
        self.provider
            .lock()
            .unwrap()
            .config
            .effort()
            .map(str::to_owned)
    }
    fn has_active_work(&self) -> bool {
        self.active.load(Ordering::Acquire)
    }
    fn can_set_model(&self) -> bool {
        self.provider
            .try_lock()
            .map(|provider| provider.acp.is_some() && provider.config.can_set_model())
            .unwrap_or(false)
    }
    fn model_change_requires_idle(&self) -> bool {
        true
    }
    fn lore_enabled(&self) -> Option<bool> {
        Some(self.memory_enabled)
    }
    fn lore_scrub_status(&self) -> Option<&'static str> {
        Some(if self.scrub_failed.load(Ordering::Acquire) {
            "unavailable"
        } else {
            "ready"
        })
    }
    fn public_prompt(&self, text: &str) -> Result<String, String> {
        self.scrub(text).map_err(|error| error.to_string())
    }
    fn transcript_snapshot(&self) -> io::Result<Option<(PathBuf, u64)>> {
        self.store.transcript_snapshot()
    }
}

fn scrub(lore: &mut LoreClient, text: &str) -> io::Result<String> {
    let text = doxa_vendors::credentials::redact(text)
        .map_err(|_| io::Error::other("Antigravity credential scrub failed"))?;
    lore.scrub(&text)
        .map_err(|_| io::Error::other("LORE scrub failed for Antigravity"))
}

fn permission_cancelled(acp: &mut Acp, id: &Value) -> io::Result<()> {
    acp.send(json!({"jsonrpc": "2.0", "id": id, "result": {"outcome": {"outcome": "cancelled"}}}))
}

fn deny_callback(acp: &mut Acp, frame: &Value, id: &Value) -> io::Result<()> {
    if frame["method"] == "session/request_permission" {
        permission_cancelled(acp, id)
    } else {
        acp.send(json!({"jsonrpc": "2.0", "id": id,
            "error": {"code": -32601, "message": "ACP client capability unavailable"}}))
    }
}

fn append_text(answer: &mut String, delta: &str, emit: &mut dyn FnMut(Value)) -> io::Result<()> {
    if answer.len() + delta.len() > MAX_ANSWER {
        return Err(io::Error::other(
            "Antigravity response exceeds DOXA transcript limit",
        ));
    }
    if !delta.is_empty() {
        answer.push_str(delta);
        let mut rest = delta;
        while !rest.is_empty() {
            let mut end = rest.len().min(8 * 1024);
            while !rest.is_char_boundary(end) {
                end -= 1;
            }
            let (chunk, remaining) = rest.split_at(end);
            emit(json!({"type": "text_delta", "data": {"text": chunk}}));
            rest = remaining;
        }
    }
    Ok(())
}

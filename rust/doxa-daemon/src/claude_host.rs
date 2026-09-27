//! Native daemon adapter for the bounded Python Claude SDK sidecar.
//! The bridge owner is a single broker thread; RPC callers never hold its lock
//! while a turn waits for events, so interrupt and answers remain available.

use doxa_claude::{Bridge, Error};
use doxa_runtime::Host;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender, SyncSender};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const RPC_TIMEOUT: Duration = Duration::from_secs(10);
const POLL: Duration = Duration::from_millis(20);
const EVENT_QUEUE: usize = 64;

enum Command {
    Prompt {
        text: String,
        events: SyncSender<Value>,
        reply: Sender<Result<Value, String>>,
    },
    Rpc {
        method: &'static str,
        params: Value,
        reply: Sender<Result<Value, String>>,
    },
    Close,
}

struct Pending {
    reply: Sender<Result<Value, String>>,
    prompt: bool,
}

pub struct ClaudeHost {
    commands: Sender<Command>,
    worker: Mutex<Option<JoinHandle<()>>>,
    active: AtomicBool,
    turn_running: Arc<AtomicBool>,
    closing: AtomicBool,
    admission: Mutex<()>,
    model_control: bool,
    effort_control: bool,
    permission_control: bool,
    reviewed_compact: bool,
    peer_tools_ready: bool,
    initial_model: Option<String>,
    initial_effort: Option<String>,
    initial_permission_mode: String,
    billing: Mutex<Option<Value>>,
    lore_enabled: Option<bool>,
    account: Mutex<Option<Value>>,
}

fn display_account(value: &Value) -> Option<Value> {
    let mut result = serde_json::Map::new();
    for key in ["email", "organization", "subscriptionType", "apiProvider"] {
        if let Some(text) = value[key].as_str().filter(|text| !text.trim().is_empty()
            && text.len() <= 256 && !text.chars().any(char::is_control)) {
            result.insert(key.into(), Value::String(text.trim().to_owned()));
        }
    }
    (!result.is_empty()).then_some(Value::Object(result))
}

impl ClaudeHost {
    pub fn new(
        python: &Path,
        script: &Path,
        cwd: &Path,
        session_id: &str,
        resume: bool,
        model: Option<&str>,
        effort: Option<&str>,
        runtime: &Path,
        spawn_depth: u32,
        parent_session_id: Option<&str>,
    ) -> Result<Self, String> {
        let mut bridge = Bridge::spawn(python, script)
            .map_err(|_| "Claude sidecar could not start".to_owned())?;
        let model_control = bridge.supports("set_model");
        let effort_control = bridge.supports("set_effort");
        let permission_control = bridge.supports("set_permission_mode");
        let reviewed_compact = bridge.supports("reviewed_compact_v1");
        let params = json!({"cwd":cwd,"session_id":session_id,
            "resume":if resume { Some(session_id) } else { None }, "model":model,"effort":effort,"lore":doxa_state::lore_enabled_default(),
            "spawn_depth":spawn_depth,"parent_session_id":parent_session_id,
            "native_spawn":{"daemon_bin":std::env::current_exe().map_err(|_| "native daemon identity unavailable")?,
                "python":python,"script":script,"runtime":runtime}});
        let id = bridge
            .request("start", params)
            .map_err(|_| "Claude sidecar start request failed".to_owned())?;
        let deadline = Instant::now() + Duration::from_secs(30);
        let start = loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err("Claude sidecar start timed out".to_owned());
            }
            match bridge.recv(remaining.min(Duration::from_secs(1))) {
                Ok(frame) if frame["type"] == "reply" && frame["id"] == id => {
                    if frame["ok"] != true {
                        if frame["error"] == "peer_presence_unsupported_update_python_and_restart" {
                            return Err("Claude Python engine is outdated; run doxa update, then restart DOXA".to_owned());
                        }
                        return Err("Claude sidecar refused session start".to_owned());
                    }
                    break frame["result"].clone();
                }
                Ok(frame) if frame["type"] == "error" => {
                    return Err("Claude sidecar protocol error".to_owned())
                }
                Ok(_) => return Err("unexpected Claude sidecar startup frame".to_owned()),
                Err(Error::Timeout) => {}
                Err(_) => return Err("Claude sidecar closed during startup".to_owned()),
            }
        };
        let lore_enabled = start["lore_enabled"].as_bool();
        if !doxa_state::lore_enabled_default() && lore_enabled != Some(false) {
            return Err("Claude sidecar did not verify memory-off; update Python and restart".into());
        }
        if (spawn_depth > 0 || parent_session_id.is_some())
            && (start["spawn_depth"].as_u64() != Some(spawn_depth as u64)
                || start["parent_session_id"].as_str() != parent_session_id
                || start["native_spawn_ready"] != true) {
            return Err("Claude sidecar did not preserve native child lineage".into());
        }
        if effort.is_some() && start["effort"].as_str() != effort {
            return Err("Claude sidecar did not apply requested startup effort".into());
        }
        let initial_effort = start["effort"].as_str().map(str::to_owned);
        let peer_tools_ready = start["peer_tools_ready"] == true;
        let initial_model = start["data"]["model"].as_str().map(str::to_owned);
        let initial_permission_mode = start["permission_mode"].as_str().unwrap_or("default");
        if !matches!(initial_permission_mode, "default" | "acceptEdits" | "plan" | "auto" | "dontAsk") {
            return Err("Claude sidecar reported an unavailable initial permission mode".into());
        }
        let initial_permission_mode = initial_permission_mode.to_owned();
        let billing = start.get("billing").and_then(validated_billing);
        let account = display_account(&start["account"]);
        let (tx, rx) = mpsc::channel();
        let turn_running = Arc::new(AtomicBool::new(false));
        let broker_turn_running = Arc::clone(&turn_running);
        let worker = thread::spawn(move || broker(bridge, rx, broker_turn_running));
        Ok(Self {
            commands: tx,
            worker: Mutex::new(Some(worker)),
            active: AtomicBool::new(false),
            turn_running,
            closing: AtomicBool::new(false),
            admission: Mutex::new(()),
            model_control,
            effort_control,
            permission_control,
            reviewed_compact,
            peer_tools_ready,
            initial_model,
            initial_effort,
            initial_permission_mode,
            billing:Mutex::new(billing), lore_enabled,
            account: Mutex::new(account),
        })
    }

    fn rpc(&self, method: &'static str, params: Value) -> Result<Value, String> {
        let (tx, rx) = mpsc::channel();
        self.commands
            .send(Command::Rpc {
                method,
                params,
                reply: tx,
            })
            .map_err(|_| "Claude sidecar closed".to_owned())?;
        let result = rx.recv_timeout(if method == "set_effort" { Duration::from_secs(60) } else { RPC_TIMEOUT })
            .map_err(|_| "Claude sidecar did not answer".to_owned())??;
        if let Some(account) = result.get("account") {
            *self.account.lock().unwrap() = display_account(account);
        }
        Ok(result)
    }

    pub fn shutdown(&self) -> bool {
        {
            let _admission = self.admission.lock().unwrap();
            self.closing.store(true, Ordering::Release);
        }
        if self.turn_active() {
            let _ = self.rpc("interrupt", json!({}));
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        while self.turn_active() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        let finalized = !self.turn_active() && self.rpc("finalize", json!({})).is_ok();
        let _ = self.commands.send(Command::Close);
        if let Some(worker) = self.worker.lock().unwrap().take() {
            let _ = worker.join();
        }
        finalized
    }

    fn turn_active(&self) -> bool {
        self.active.load(Ordering::Acquire) || self.turn_running.load(Ordering::Acquire)
    }
}

impl Host for ClaudeHost {
    fn has_active_work(&self) -> bool { self.turn_active() }
    fn lore_enabled(&self) -> Option<bool> { self.lore_enabled }
    fn peer_tools_ready(&self) -> bool { self.peer_tools_ready && !self.closing.load(Ordering::Acquire) }
    fn can_set_model(&self) -> bool { self.model_control }
    fn can_set_permission_mode(&self) -> bool { self.permission_control }
    fn initial_model(&self) -> Option<String> { self.initial_model.clone() }
    fn initial_effort(&self) -> Option<String> { self.initial_effort.clone() }
    fn initial_permission_mode(&self) -> String { self.initial_permission_mode.clone() }
    fn billing_snapshot(&self) -> Option<Value> { self.billing.lock().ok()?.clone() }
    fn account_snapshot(&self) -> Option<Value> { self.account.lock().unwrap().clone() }
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
        if text.trim_start().starts_with("/compact") && !self.reviewed_compact {
            emit(done("Reviewed compaction is unavailable in this Claude sidecar"));
            return;
        }
        let (events_tx, events_rx) = mpsc::sync_channel(EVENT_QUEUE);
        let (reply_tx, reply_rx) = mpsc::channel();
        let admitted = {
            let _admission = self.admission.lock().unwrap();
            if self.closing.load(Ordering::Acquire) {
                Err("Claude session is stopping")
            } else if self.active.swap(true, Ordering::AcqRel) {
                Err("Claude turn already running")
            } else if self
                .commands
                .send(Command::Prompt {
                    text: text.to_owned(),
                    events: events_tx,
                    reply: reply_tx,
                })
                .is_err()
            {
                self.active.store(false, Ordering::Release);
                Err("Claude sidecar closed")
            } else {
                Ok(())
            }
        };
        if let Err(reason) = admitted {
            emit(done(reason));
            return;
        }
        let timeout = if text.trim() == "/compact" { Duration::from_secs(195) } else { RPC_TIMEOUT };
        match reply_rx.recv_timeout(timeout) {
            Ok(Ok(_)) => {}
            _ => {
                self.active.store(false, Ordering::Release);
                emit(done(if text.trim_start().starts_with("/compact") {
                    "LORE review failed or timed out; compaction refused"
                } else { "Claude sidecar refused prompt" }));
                return;
            }
        }
        loop {
            match events_rx.recv_timeout(Duration::from_secs(2)) {
                Ok(mut event) => {
                    if event["type"] == "billing" {
                        let Some(billing)=validated_billing(&event["data"]) else { continue; };
                        event["data"]=billing.clone();
                        if let Ok(mut current)=self.billing.lock() { *current=Some(billing); }
                    }
                    let terminal =
                        matches!(event["type"].as_str(), Some("turn_done" | "turn_refused"));
                    emit(event);
                    if terminal {
                        break;
                    }
                }
                Err(RecvTimeoutError::Disconnected) => {
                    emit(done("Claude event stream closed"));
                    break;
                }
                Err(RecvTimeoutError::Timeout) if self.closing.load(Ordering::Acquire) => {
                    emit(done("Claude session stopped"));
                    break;
                }
                Err(RecvTimeoutError::Timeout) => {}
            }
        }
        self.active.store(false, Ordering::Release);
    }

    fn call(&self, method: &str, params: &Value) -> Result<Value, String> {
        match method {
            "context_detail" => {
                let result = self.rpc("context_detail", json!({}))?;
                if !result.is_object() { return Err("Claude context detail is unavailable".into()); }
                Ok(result)
            }
            "list_models" => {
                if !self.model_control { return Err("Claude sidecar does not support set_model".into()); }
                self.rpc("list_models", json!({}))
            }
            "set_model" => {
                if !self.model_control {
                    return Err("Claude sidecar does not support set_model".into());
                }
                let model = params.get("model").ok_or("model is required")?;
                if !model.is_null() && (model.as_str().is_none_or(|s| s.trim().is_empty()
                    || s.len() > 128 || s.chars().any(char::is_control))) {
                    return Err("invalid model".into());
                }
                let result = self.rpc("set_model", json!({"model":model}))?;
                let selected = if model.is_null() && result["model"].is_null() {
                    "default"
                } else {
                    result["model"].as_str().ok_or("invalid model reply")?
                };
                Ok(json!({"model":selected}))
            }
            "set_effort" => {
                let _admission = self.admission.lock().unwrap();
                if !self.effort_control { return Err("Claude sidecar does not support effort resume".into()); }
                if self.turn_active() || self.closing.load(Ordering::Acquire) {
                    return Err("Claude effort changes require an idle session".into());
                }
                let effort = params["effort"].as_str().ok_or("effort is required")?;
                if !matches!(effort, "low" | "medium" | "high" | "xhigh" | "max") {
                    return Err("unsupported Claude effort".into());
                }
                self.rpc("set_effort", json!({"effort":effort}))
            }
            "set_permission_mode" => {
                if !self.permission_control {
                    return Err("Claude sidecar does not support set_permission_mode".into());
                }
                let mode = params["mode"].as_str().ok_or("mode is required")?;
                if !matches!(mode, "default" | "acceptEdits" | "plan" | "auto" | "dontAsk") {
                    return Err("invalid or unavailable permission mode".into());
                }
                let result = {
                    let _admission = self.admission.lock().unwrap();
                    if mode == "dontAsk" && self.active.load(Ordering::Acquire) {
                        return Err("dontAsk requires an idle Claude turn".into());
                    }
                    self.rpc("set_permission_mode", json!({"mode":mode}))?
                };
                if result["mode"] != mode {
                    return Err("invalid permission mode reply".into());
                }
                Ok(json!({"mode":mode}))
            }
            "answer_needs_input" => {
                let id = params["id"]
                    .as_str()
                    .ok_or_else(|| "answer needs an id".to_owned())?;
                let answer = params["answer"]
                    .as_object()
                    .ok_or_else(|| "answer must be an object".to_owned())?;
                let result = self.rpc("answer", json!({"id":id,"answer":answer}))?;
                Ok(json!({"applied":result["applied"]}))
            }
            "interrupt" => {
                self.rpc("interrupt", json!({}))?;
                Ok(json!({}))
            }
            "stop" => {
                {
                    let _admission = self.admission.lock().unwrap();
                    self.closing.store(true, Ordering::Release);
                }
                if self.turn_active() {
                    let _ = self.rpc("interrupt", json!({}));
                }
                Ok(json!({}))
            }
            _ => Err(format!("{method} is unavailable in the native Claude host")),
        }
    }
}

fn done(message: &str) -> Value {
    json!({"type":"turn_done","data":{"is_error":true,"error":message}})
}

fn broker(bridge: Bridge, commands: Receiver<Command>, turn_running: Arc<AtomicBool>) {
    broker_loop(bridge, commands, &turn_running);
    turn_running.store(false, Ordering::Release);
}

fn broker_loop(mut bridge: Bridge, commands: Receiver<Command>, turn_running: &AtomicBool) {
    let mut pending: HashMap<u64, Pending> = HashMap::new();
    let mut events: Option<SyncSender<Value>> = None;
    loop {
        for _ in 0..8 {
            match commands.try_recv() {
                Ok(Command::Prompt {
                    text,
                    events: sink,
                    reply,
                }) => {
                    if turn_running.load(Ordering::Acquire) {
                        let _ = reply.send(Err("Claude turn already running".into()));
                        continue;
                    }
                    match bridge.request("prompt", json!({"text":text})) {
                        Ok(id) => {
                            turn_running.store(true, Ordering::Release);
                            events = Some(sink);
                            pending.insert(
                                id,
                                Pending {
                                    reply,
                                    prompt: true,
                                },
                            );
                        }
                        Err(_) => {
                            let _ = reply.send(Err("Claude prompt request failed".into()));
                            return;
                        }
                    }
                }
                Ok(Command::Rpc {
                    method,
                    params,
                    reply,
                }) => {
                    if method == "set_permission_mode"
                        && params["mode"] == "dontAsk"
                        && turn_running.load(Ordering::Acquire)
                    {
                        let _ = reply.send(Err("dontAsk requires an idle Claude turn".into()));
                        continue;
                    }
                    match bridge.request(method, params) {
                        Ok(id) => {
                            pending.insert(
                                id,
                                Pending {
                                    reply,
                                    prompt: false,
                                },
                            );
                        }
                        Err(_) => {
                            let _ = reply.send(Err("Claude request failed".into()));
                            return;
                        }
                    }
                }
                Ok(Command::Close) | Err(mpsc::TryRecvError::Disconnected) => return,
                Err(mpsc::TryRecvError::Empty) => break,
            }
        }
        match bridge.recv(POLL) {
            Ok(frame) if frame["type"] == "reply" => {
                let Some(id) = frame["id"].as_u64() else {
                    return;
                };
                let Some(pending_reply) = pending.remove(&id) else {
                    // A caller may have timed out; duplicate or late replies do
                    // not invalidate other pending requests or the event stream.
                    continue;
                };
                if frame["ok"] == true {
                    let _ = pending_reply.reply.send(Ok(frame["result"].clone()));
                } else {
                    let _ = pending_reply
                        .reply
                        .send(Err("Claude sidecar operation failed".into()));
                    if pending_reply.prompt {
                        events = None;
                        turn_running.store(false, Ordering::Release);
                    }
                }
            }
            Ok(frame) if frame["type"] == "event" => {
                let Some(kind) = frame["event"].as_str() else {
                    return;
                };
                let event = if kind == "turn_interrupted" {
                    done("Claude turn interrupted")
                } else {
                    json!({"type":kind,"data":frame["data"]})
                };
                let terminal = matches!(event["type"].as_str(), Some("turn_done" | "turn_refused"));
                if let Some(sink) = &events {
                    if sink.try_send(event).is_err() {
                        events = None;
                    }
                }
                if terminal {
                    events = None;
                    turn_running.store(false, Ordering::Release);
                }
            }
            Ok(frame) if frame["type"] == "error" => return,
            Ok(_) => return,
            Err(Error::Timeout) => {}
            Err(_) => return,
        }
    }
}

/// Only bounded non-secret subscription telemetry crosses the provider boundary.
fn validated_billing(value: &Value) -> Option<Value> {
    if value["mode"] != "subscription" { return None; }
    let tier=value["type"].as_str().filter(|tier|!tier.is_empty()&&tier.len()<=64&&!tier.chars().any(char::is_control))?;
    let quota=value.get("quota").unwrap_or(&Value::Null);
    if !quota.is_null() && quota.as_str().is_none_or(|text|text.len()>120||text.chars().any(char::is_control)) { return None; }
    let mut result=json!({"mode":"subscription","type":tier,"quota":quota});
    if let Some(limits)=value.get("quota_limits") {
        let rows=limits.as_object().filter(|rows|rows.len()<=4)?;
        let mut projected=serde_json::Map::new();
        for (window,row) in rows {
            if !matches!(window.as_str(),"five_hour"|"seven_day"|"seven_day_opus"|"seven_day_sonnet") || !row.is_object() { return None; }
            let mut clean=serde_json::Map::new();
            if let Some(percent)=row.get("percent") { if percent.as_u64().is_none_or(|value|value>100) { return None; } clean.insert("percent".into(),percent.clone()); }
            if let Some(status)=row.get("status") { if !matches!(status.as_str(),Some("allowed"|"allowed_warning"|"rejected")) { return None; } clean.insert("status".into(),status.clone()); }
            if let Some(reset)=row.get("resets_at") { if reset.as_u64().is_none_or(|value|value>253402300799) { return None; } clean.insert("resets_at".into(),reset.clone()); }
            if let Some(source)=row.get("source") { if !matches!(source.as_str(),Some("sdk"|"claude_cli_cache")) { return None; } clean.insert("source".into(),source.clone()); }
            if let Some(stale)=row.get("stale") { if !stale.is_boolean() { return None; } clean.insert("stale".into(),stale.clone()); }
            projected.insert(window.clone(),Value::Object(clean));
        }
        result["quota_limits"]=Value::Object(projected);
    }
    if let Some(source)=value.get("quota_source") { if !matches!(source.as_str(),Some("sdk"|"claude_cli_cache"|"mixed")) { return None; } result["quota_source"]=source.clone(); }
    if let Some(stale)=value.get("quota_stale") { if !stale.is_boolean() { return None; } result["quota_stale"]=stale.clone(); }
    Some(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::{mpsc, Arc};

    #[test]
    fn connected_account_snapshot_is_bounded_and_never_forwards_credentials() {
        assert_eq!(display_account(&json!({"email":" sdk@example.test ","organization":"SDK org",
            "accessToken":"secret","organizationName":"foreign cached org"})),
            Some(json!({"email":"sdk@example.test","organization":"SDK org"})));
        assert!(display_account(&json!({"email":"bad\nvalue","organization":"x".repeat(257)})).is_none());
    }

    fn fixture(script: &str) -> (tempfile::TempDir, Arc<ClaudeHost>) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sidecar.py");
        fs::write(&path, script).unwrap();
        let host = ClaudeHost::new(Path::new("python3"), &path, dir.path(), "test", false, None, None, dir.path(), 0, None)
            .unwrap();
        (dir, Arc::new(host))
    }

    #[test]
    fn reported_subscription_quota_updates_cached_billing_and_filters_raw_fields() {
        let (_dir,host)=fixture(r#"import json,sys
print(json.dumps({"type":"hello","protocol":"doxa-claude-sidecar","version":1,"capabilities":[]}),flush=True)
for line in sys.stdin:
    frame=json.loads(line)
    result={"data":{"model":"opus"},"billing":{"mode":"subscription","type":"max 20x","quota":"5h:9% week:48%~"}} if frame["method"]=="start" else {}
    print(json.dumps({"type":"reply","id":frame["id"],"ok":True,"result":result}),flush=True)
    if frame["method"]=="prompt":
        for data in [{"mode":"subscription","type":"max 20x","quota":"5h:23% week:61%","raw":"private account data","quota_limits":{"five_hour":{"percent":23,"status":"allowed_warning","source":"sdk","stale":False},"seven_day":{"percent":61,"source":"sdk","stale":False}},"quota_source":"sdk","quota_stale":False},
                     {"mode":"subscription","type":"max 20x","quota":"invalid","quota_limits":{"five_hour":{"percent":101}}}]:
            print(json.dumps({"type":"event","event":"billing","data":data}),flush=True)
        print(json.dumps({"type":"event","event":"turn_done","data":{}}),flush=True)
    if frame["method"]=="finalize": break
"#);
        assert_eq!(host.billing_snapshot().unwrap()["quota"],"5h:9% week:48%~");
        let mut events=Vec::new();
        host.prompt("scripted quota update",&mut |event| {
            if event["type"]=="billing" { assert_eq!(host.billing_snapshot().unwrap()["quota"],"5h:23% week:61%"); }
            events.push(event);
        });
        let billing:Vec<_>=events.iter().filter(|row|row["type"]=="billing").collect();
        assert_eq!(billing.len(),1); assert!(billing[0]["data"].get("raw").is_none());
        assert_eq!(host.billing_snapshot().unwrap()["quota_limits"]["seven_day"]["percent"],61);
        host.shutdown();
    }

    #[test]
    fn rejected_prompt_does_not_clear_running_turn_and_late_reply_is_ignored() {
        let (_dir, host) = fixture(r#"import json, sys
print(json.dumps({"type":"hello","protocol":"doxa-claude-sidecar","version":1,
                  "capabilities":["set_model","set_permission_mode"]}), flush=True)
for line in sys.stdin:
    frame = json.loads(line)
    method, ident = frame["method"], frame["id"]
    result = {"data":{"model":"opus"},"permission_mode":"plan"} if method == "start" else {}
    if method == "set_model": result = {"model":None,"account":{"email":"reconnected@example.test","accessToken":"secret"}}
    if method == "set_permission_mode": result = {"mode":frame["params"]["mode"]}
    print(json.dumps({"type":"reply","id":ident,"ok":True,"result":result}), flush=True)
    if method == "prompt":
        print(json.dumps({"type":"event","event":"needs_input","data":{"id":"q"}}), flush=True)
    if method == "set_model":
        print(json.dumps({"type":"reply","id":ident,"ok":True,"result":result}), flush=True)
    if method == "interrupt":
        print(json.dumps({"type":"event","event":"turn_done","data":{}}), flush=True)
"#);
        assert_eq!(host.call("set_model", &json!({"model":null})).unwrap()["model"], "default");
        assert_eq!(host.account_snapshot(), Some(json!({"email":"reconnected@example.test"})));
        // The duplicate set_model reply must not kill the broker.
        assert_eq!(host.call("set_permission_mode", &json!({"mode":"plan"})).unwrap()["mode"], "plan");
        let (seen_tx, seen_rx) = mpsc::channel();
        let running = Arc::clone(&host);
        let turn = thread::spawn(move || running.prompt("first", &mut |event| {
            if event["type"] == "needs_input" { let _ = seen_tx.send(()); }
        }));
        seen_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        let mut rejected = Vec::new();
        host.prompt("second", &mut |event| rejected.push(event));
        assert_eq!(rejected.len(), 1);
        assert_eq!(rejected[0]["data"]["error"], "Claude turn already running");
        assert!(host.active.load(Ordering::Acquire));
        assert!(host.call("set_permission_mode", &json!({"mode":"dontAsk"})).is_err());
        host.call("interrupt", &json!({})).unwrap();
        turn.join().unwrap();
        assert!(host.shutdown());
    }

    #[test]
    fn full_event_queue_keeps_sidecar_turn_busy_until_terminal() {
        let (_dir, host) = fixture(r#"import json, sys, time
print(json.dumps({"type":"hello","protocol":"doxa-claude-sidecar","version":1,
                  "capabilities":["set_permission_mode"]}), flush=True)
for line in sys.stdin:
    frame = json.loads(line)
    method, ident = frame["method"], frame["id"]
    result = {"permission_mode":"plan"} if method == "start" else {}
    if method == "set_permission_mode": result = {"mode":frame["params"]["mode"]}
    print(json.dumps({"type":"reply","id":ident,"ok":True,"result":result}), flush=True)
    if method == "prompt":
        for i in range(200):
            print(json.dumps({"type":"event","event":"text_delta","data":{"text":str(i)}}), flush=True)
        time.sleep(0.5)
        print(json.dumps({"type":"event","event":"turn_done","data":{}}), flush=True)
"#);
        let mut terminal = Value::Null;
        host.prompt("first", &mut |event| {
            if event["type"] == "text_delta" { thread::sleep(Duration::from_millis(2)); }
            if event["type"] == "turn_done" { terminal = event; }
        });
        assert_eq!(terminal["data"]["error"], "Claude event stream closed");
        assert!(host.call("set_permission_mode", &json!({"mode":"dontAsk"})).is_err());
        let mut rejected = Vec::new();
        host.prompt("too early", &mut |event| rejected.push(event));
        assert_eq!(rejected[0]["data"]["error"], "Claude sidecar refused prompt");
        // Queue saturation closes only this event receiver. The broker waits
        // for the sidecar terminal before admitting a new turn.
        thread::sleep(Duration::from_millis(600));
        let mut second = Vec::new();
        host.prompt("second", &mut |event| second.push(event));
        assert_eq!(second.last().unwrap()["type"], "turn_done");
        assert!(host.shutdown());
    }
    #[test]
    fn native_child_start_requires_exact_depth_parent_and_native_route_echo() {
        let dir = tempfile::tempdir().unwrap();
        for valid in [false, true] {
            let script = dir.path().join("lineage.py");
            fs::write(&script, format!(r#"import json, sys
print(json.dumps({{"type":"hello","protocol":"doxa-claude-sidecar","version":1,"capabilities":["start"]}}),flush=True)
for line in sys.stdin:
 r=json.loads(line)
 p=r['params']
 assert p['spawn_depth']==2 and p['parent_session_id']=='parent-123'
 assert p['effort']=='high'
 assert p['native_spawn']['runtime']=={runtime:?}
 assert p['native_spawn']['daemon_bin'].startswith('/')
 result={{"spawn_depth":{depth},"parent_session_id":"parent-123","native_spawn_ready":True,"permission_mode":"default","effort":"high"}}
 print(json.dumps({{"type":"reply","id":r['id'],"ok":True,"result":result}}),flush=True)
"#, runtime=dir.path().to_str().unwrap(),depth=if valid { 2 } else { 0 })).unwrap();
            let result = ClaudeHost::new(Path::new("python3"), &script, dir.path(), "child", false,
                None, Some("high"), dir.path(), 2, Some("parent-123"));
            assert_eq!(result.is_ok(), valid);
            if let Ok(host) = result { assert_eq!(host.initial_effort(), Some("high".into())); }
        }
    }

}

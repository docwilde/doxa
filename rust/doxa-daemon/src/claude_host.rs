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
    permission_control: bool,
    initial_model: Option<String>,
    initial_permission_mode: String,
}

impl ClaudeHost {
    pub fn new(
        python: &Path,
        script: &Path,
        cwd: &Path,
        session_id: &str,
        resume: bool,
        model: Option<&str>,
    ) -> Result<Self, String> {
        let mut bridge = Bridge::spawn(python, script)
            .map_err(|_| "Claude sidecar could not start".to_owned())?;
        let model_control = bridge.supports("set_model");
        let permission_control = bridge.supports("set_permission_mode");
        let params = json!({"cwd":cwd,"session_id":session_id,
            "resume":if resume { Some(session_id) } else { None }, "model":model});
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
        let initial_model = start["data"]["model"].as_str().map(str::to_owned);
        let initial_permission_mode = start["permission_mode"].as_str().unwrap_or("default");
        if !matches!(initial_permission_mode, "default" | "acceptEdits" | "plan" | "auto" | "dontAsk") {
            return Err("Claude sidecar reported an unavailable initial permission mode".into());
        }
        let initial_permission_mode = initial_permission_mode.to_owned();
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
            permission_control,
            initial_model,
            initial_permission_mode,
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
        rx.recv_timeout(RPC_TIMEOUT)
            .map_err(|_| "Claude sidecar did not answer".to_owned())?
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
    fn can_set_model(&self) -> bool { self.model_control }
    fn can_set_permission_mode(&self) -> bool { self.permission_control }
    fn initial_model(&self) -> Option<String> { self.initial_model.clone() }
    fn initial_permission_mode(&self) -> String { self.initial_permission_mode.clone() }
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
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
        match reply_rx.recv_timeout(RPC_TIMEOUT) {
            Ok(Ok(_)) => {}
            _ => {
                self.active.store(false, Ordering::Release);
                emit(done("Claude sidecar refused prompt"));
                return;
            }
        }
        loop {
            match events_rx.recv_timeout(Duration::from_secs(2)) {
                Ok(event) => {
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::{mpsc, Arc};

    fn fixture(script: &str) -> (tempfile::TempDir, Arc<ClaudeHost>) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sidecar.py");
        fs::write(&path, script).unwrap();
        let host = ClaudeHost::new(Path::new("python3"), &path, dir.path(), "test", false, None)
            .unwrap();
        (dir, Arc::new(host))
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
    if method == "set_model": result = {"model":None}
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
}

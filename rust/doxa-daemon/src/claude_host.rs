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
use std::sync::Mutex;
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
    closing: AtomicBool,
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
        let params = json!({"cwd":cwd,"session_id":session_id,
            "resume":if resume { Some(session_id) } else { None }, "model":model});
        let id = bridge
            .request("start", params)
            .map_err(|_| "Claude sidecar start request failed".to_owned())?;
        let deadline = Instant::now() + Duration::from_secs(30);
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err("Claude sidecar start timed out".to_owned());
            }
            match bridge.recv(remaining.min(Duration::from_secs(1))) {
                Ok(frame) if frame["type"] == "reply" && frame["id"] == id => {
                    if frame["ok"] != true {
                        return Err("Claude sidecar refused session start".to_owned());
                    }
                    break;
                }
                Ok(frame) if frame["type"] == "error" => {
                    return Err("Claude sidecar protocol error".to_owned())
                }
                Ok(_) => return Err("unexpected Claude sidecar startup frame".to_owned()),
                Err(Error::Timeout) => {}
                Err(_) => return Err("Claude sidecar closed during startup".to_owned()),
            }
        }
        let (tx, rx) = mpsc::channel();
        let worker = thread::spawn(move || broker(bridge, rx));
        Ok(Self {
            commands: tx,
            worker: Mutex::new(Some(worker)),
            active: AtomicBool::new(false),
            closing: AtomicBool::new(false),
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
        self.closing.store(true, Ordering::Release);
        if self.active.load(Ordering::Acquire) {
            let _ = self.rpc("interrupt", json!({}));
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        while self.active.load(Ordering::Acquire) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        let finalized =
            !self.active.load(Ordering::Acquire) && self.rpc("finalize", json!({})).is_ok();
        let _ = self.commands.send(Command::Close);
        if let Some(worker) = self.worker.lock().unwrap().take() {
            let _ = worker.join();
        }
        finalized
    }
}

impl Host for ClaudeHost {
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
        if self.closing.load(Ordering::Acquire) {
            emit(done("Claude session is stopping"));
            return;
        }
        self.active.store(true, Ordering::Release);
        let (events_tx, events_rx) = mpsc::sync_channel(EVENT_QUEUE);
        let (reply_tx, reply_rx) = mpsc::channel();
        if self
            .commands
            .send(Command::Prompt {
                text: text.to_owned(),
                events: events_tx,
                reply: reply_tx,
            })
            .is_err()
        {
            self.active.store(false, Ordering::Release);
            emit(done("Claude sidecar closed"));
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
                self.closing.store(true, Ordering::Release);
                if self.active.load(Ordering::Acquire) {
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

fn broker(mut bridge: Bridge, commands: Receiver<Command>) {
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
                    if events.is_some() {
                        let _ = reply.send(Err("Claude turn already running".into()));
                        continue;
                    }
                    match bridge.request("prompt", json!({"text":text})) {
                        Ok(id) => {
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
                }) => match bridge.request(method, params) {
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
                },
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
                    return;
                };
                if frame["ok"] == true {
                    let _ = pending_reply.reply.send(Ok(frame["result"].clone()));
                } else {
                    let _ = pending_reply
                        .reply
                        .send(Err("Claude sidecar operation failed".into()));
                    if pending_reply.prompt {
                        events = None;
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
                }
            }
            Ok(frame) if frame["type"] == "error" => return,
            Ok(_) => return,
            Err(Error::Timeout) => {}
            Err(_) => return,
        }
    }
}

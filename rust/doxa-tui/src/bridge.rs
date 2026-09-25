//! Connect one daemon socket to the terminal loop without blocking input.

use std::collections::{HashMap, HashSet};
use std::io;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::mpsc::{self, Receiver, SyncSender, TryRecvError};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::transport::{DaemonClient, TransportError};
use crate::history;
use crate::ui;
use crate::discovery::Session;
use crate::launch::{self, LaunchOptions};
use std::path::PathBuf;

#[derive(Debug)]
pub enum WorkerCommand {
    Launch(LaunchOptions, Option<String>, usize),
    Attach(String, usize),
    Prompt(String, String),
    Answer(String, String, Value),
    Peers(String),
    Message(String, String, String),
    Models(String),
    SetModel(String, String),
    SetPermissionMode(String, String),
    QueueList(String),
    QueueCancel(String, String),
    Stop(String),
}

fn safe_queue_rows(reply: &Value) -> Vec<Value> {
    let python = std::env::var_os("DOXA_LORE_PYTHON").map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("python3"));
    safe_queue_rows_with_python(reply, &python)
}

fn safe_queue_rows_with_python(reply: &Value, python: &Path) -> Vec<Value> {
    let Some(rows) = reply["queue"].as_array() else { return Vec::new(); };
    let mut lore = doxa_lore::LoreClient::spawn(&python, Duration::from_secs(2)).ok();
    rows.iter().take(64).filter_map(|row| {
        let id = row["id"].as_str()?;
        if id.is_empty() || id.len() > 128 || !id.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-') {
            return None;
        }
        let preview = row["text"].as_str().filter(|text| text.len() <= 64 * 1024)
            .and_then(|text| lore.as_mut()?.scrub(text).ok())
            .map(|text| text.chars().take(160).collect::<String>())
            .unwrap_or_else(|| "[preview unavailable]".into());
        Some(json!({"id":id,"preview":preview}))
    }).collect()
}

fn attach_worker(session: &Session, frames: &SyncSender<Value>, guard: &Arc<Mutex<bool>>)
    -> io::Result<(SyncSender<WorkerCommand>, Arc<AtomicBool>, JoinHandle<()>)> {
    let (client, snapshot) = DaemonClient::connect_for_restore(&session.socket).map_err(io::Error::other)?;
    if client.hello["session_id"] != session.id {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "session identity changed during attach"));
    }
    let (tx, rx) = mpsc::sync_channel(32);
    let connected = Arc::new(AtomicBool::new(true));
    let connected_worker = Arc::clone(&connected);
    let path = session.socket.clone();
    let id = session.id.clone();
    let frames = frames.clone();
    let guard = Arc::clone(guard);
    let worker = thread::spawn(move || {
        let cursor = AtomicU64::new(client.cursor);
        let stopped = AtomicBool::new(false);
        let mut current = (client, snapshot);
        loop {
            worker_loop(current.0, current.1, &frames, &rx, &cursor, Some(&guard), &stopped);
            connected_worker.store(false, Ordering::Release);
            if stopped.load(Ordering::Acquire) {
                revoke(&guard);
                while let Ok(command) = rx.try_recv() {
                    let _ = frames.send(rejected(command, "Session is stopping"));
                }
                return;
            }
            revoke(&guard);
            if frames.send(json!({"type":"client_notice", "session_id":id,
                "message":"Daemon disconnected; reconnecting"})).is_err() { return; }
            loop {
                match rx.recv_timeout(Duration::from_millis(250)) {
                    Err(mpsc::RecvTimeoutError::Disconnected) => return,
                    Ok(command) => { let _ = frames.send(rejected(command, "Daemon reconnecting")); }
                    Err(mpsc::RecvTimeoutError::Timeout) => {}
                }
                match DaemonClient::connect(&path, Some(cursor.load(Ordering::Relaxed))) {
                    Ok(client) if client.hello["session_id"] == id => {
                        connected_worker.store(true, Ordering::Release);
                        current = (client, None);
                        break;
                    }
                    _ => {}
                }
            }
        }
    });
    Ok((tx, connected, worker))
}

fn revoke(guard: &Mutex<bool>) {
    *guard.lock().unwrap_or_else(|poison| poison.into_inner()) = false;
}

/// Each session owns its socket, replay cursor, and bounded command queue.
/// `complete` becomes false on any failed attach or disconnect and stays
/// false for this UI lifetime, so a partial roster never rewrites a tabset.
pub struct MultiBridge {
    pub frames: Receiver<Value>,
    pub commands: SyncSender<WorkerCommand>,
    pub live_ids: Vec<String>,
    pub complete: Arc<Mutex<bool>>,
    router: JoinHandle<()>,
    workers: Vec<JoinHandle<()>>,
}

impl MultiBridge {
    pub fn shutdown(self) {
        drop(self.commands);
        drop(self.frames);
        let _ = self.router.join();
        for worker in self.workers { let _ = worker.join(); }
    }
}

pub fn connect_sessions(sessions: &[Session]) -> io::Result<MultiBridge> {
    if sessions.is_empty() || sessions.len() > 64 {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "expected 1–64 live sessions"));
    }
    let mut seen = HashSet::new();
    if sessions.iter().any(|s| !crate::discovery::valid_id(&s.id) || !seen.insert(s.id.as_str())) {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid or duplicate session ID in roster"));
    }
    let (frame_tx, frame_rx) = mpsc::sync_channel(128);
    let (command_tx, command_rx) = mpsc::sync_channel(32);
    let complete = Arc::new(Mutex::new(true));
    let mut routes = HashMap::new();
    let mut workers = Vec::new();
    let mut live_ids = Vec::new();
    for session in sessions {
        let (tx, connected, worker) = match attach_worker(session, &frame_tx, &complete) {
            Ok(pair) => pair,
            _ => {
                revoke(&complete);
                let _ = frame_tx.send(json!({"type":"client_notice", "session_id":session.id,
                    "message":"Session unavailable; saved layout is read-only"}));
                continue;
            }
        };
        routes.insert(session.id.clone(), (tx, Arc::clone(&connected)));
        live_ids.push(session.id.clone());
        workers.push(worker);
    }
    if live_ids.is_empty() {
        drop(command_rx);
        drop(frame_tx);
        for worker in workers { let _ = worker.join(); }
        return Err(io::Error::new(io::ErrorKind::NotConnected, "no daemon in roster accepted an attach"));
    }
    let router_frames = frame_tx.clone();
    let router_guard = Arc::clone(&complete);
    let router = thread::spawn(move || {
        let (launched_tx, launched_rx) = mpsc::channel::<(io::Result<Session>, Option<String>, usize)>();
        let mut added_workers: Vec<JoinHandle<()>> = Vec::new();
        let mut launches_in_flight = 0usize;
        loop {
            while let Ok((result, prompt, group)) = launched_rx.try_recv() {
                launches_in_flight = launches_in_flight.saturating_sub(1);
                match result {
                    Ok(session) => match attach_worker(&session, &router_frames, &router_guard) {
                    Ok((tx, connected, worker)) => {
                        let id = session.id.clone();
                        routes.insert(id.clone(), (tx.clone(), connected));
                        added_workers.push(worker);
                        let _ = router_frames.send(json!({"type":"launch_reply", "ok":true,
                            "session_id":id, "group":group}));
                        if let Some(prompt) = prompt {
                            let _ = tx.send(WorkerCommand::Prompt(id, prompt));
                        }
                    }
                    Err(error) => { let _ = router_frames.send(json!({"type":"launch_reply", "ok":false,
                        "started":true, "session_id":session.id, "group":group,
                        "message":format!("UI attach failed ({error}); use doxa-rs attach {}", session.id)})); }
                    },
                    Err(error) => { let _ = router_frames.send(json!({"type":"launch_reply", "ok":false,
                        "message":error.to_string(), "group":group})); }
                }
            }
            let command = match command_rx.recv_timeout(Duration::from_millis(50)) {
                Ok(command) => command,
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            };
            let command = match command {
                WorkerCommand::Attach(id, group) => {
                    if routes.contains_key(&id) {
                        let _ = router_frames.send(json!({"type":"attach_reply", "ok":true,
                            "session_id":id, "group":group}));
                        continue;
                    }
                    if routes.len() >= 64 {
                        let _ = router_frames.send(json!({"type":"attach_reply", "ok":false,
                            "message":"64 attached sessions is the limit"}));
                        continue;
                    }
                    // Re-read the trusted registry at dispatch time. The UI's earlier
                    // discovery result is only a selection hint, never a socket path.
                    let live = crate::discovery::sessions();
                    let session = live.as_ref().ok().and_then(|rows| rows.iter().find(|s| s.id == id));
                    let result = session.ok_or_else(|| io::Error::new(io::ErrorKind::NotFound,
                        "session is no longer live")).and_then(|s| attach_worker(s, &router_frames, &router_guard));
                    match result {
                        Ok((tx, connected, worker)) => {
                            routes.insert(id.clone(), (tx, connected));
                            added_workers.push(worker);
                            let _ = router_frames.send(json!({"type":"attach_reply", "ok":true,
                                "session_id":id, "group":group}));
                        }
                        Err(error) => { let _ = router_frames.send(json!({"type":"attach_reply", "ok":false,
                            "message":error.to_string()})); }
                    }
                    continue;
                }
                WorkerCommand::Launch(options, prompt, group) => {
                    if routes.len().saturating_add(launches_in_flight) >= 64 {
                        let _ = router_frames.send(json!({"type":"launch_reply", "ok":false,
                            "message":"64 attached sessions is the limit", "group":group}));
                        continue;
                    }
                    launches_in_flight += 1;
                    let reply = launched_tx.clone();
                    thread::spawn(move || { let _ = reply.send((launch::spawn(&options), prompt, group)); });
                    continue;
                }
                other => other,
            };
            let id = match &command {
                WorkerCommand::Prompt(id, _) | WorkerCommand::Answer(id, _, _) | WorkerCommand::Peers(id)
                | WorkerCommand::Models(id) | WorkerCommand::SetModel(id, _)
                | WorkerCommand::SetPermissionMode(id, _) | WorkerCommand::QueueList(id)
                | WorkerCommand::QueueCancel(id, _) => id,
                WorkerCommand::Message(id, _, _) | WorkerCommand::Stop(id) => id,
                WorkerCommand::Launch(_, _, _) | WorkerCommand::Attach(_, _) => unreachable!(),
            };
            let Some((route, connected)) = routes.get(id) else {
                let _ = router_frames.send(rejected(command, "Session is not attached"));
                continue;
            };
            if !connected.load(Ordering::Acquire) {
                let _ = router_frames.send(rejected(command, "Daemon reconnecting"));
                continue;
            }
            match route.try_send(command) {
                Ok(()) => {}
                Err(mpsc::TrySendError::Full(command)) => {
                    let _ = router_frames.send(rejected(command, "Daemon command queue full"));
                }
                Err(mpsc::TrySendError::Disconnected(command)) => {
                    let _ = router_frames.send(rejected(command, "Daemon worker unavailable"));
                }
            }
        }
        drop(routes);
        for worker in added_workers { let _ = worker.join(); }
    });
    drop(frame_tx);
    Ok(MultiBridge { frames: frame_rx, commands: command_tx, live_ids, complete, router, workers })
}

fn rejected(command: WorkerCommand, message: &str) -> Value {
    match command {
        WorkerCommand::Launch(_, _, group) => json!({"type":"launch_reply", "ok":false,
            "message":message, "group":group}),
        WorkerCommand::Attach(id, group) => json!({"type":"attach_reply", "ok":false,
            "session_id":id, "message":message, "group":group}),
        WorkerCommand::Prompt(id, text) => json!({"type":"prompt_rejected", "session_id":id,
            "text":text, "message":message}),
        WorkerCommand::Answer(session, request, _) => json!({"type":"answer_reply", "session_id":session,
            "request_id":request, "ok":false, "message":message}),
        WorkerCommand::Peers(id) => json!({"type":"peer_roster", "session_id":id,
            "ok":false, "error":message}),
        WorkerCommand::Message(id, target, text) => json!({"type":"peer_message_reply", "session_id":id,
            "ok":false, "uncertain":false, "error":message,
            "draft":format!("/msg {target} {text}")}),
        WorkerCommand::Models(id) => json!({"type":"models_reply", "session_id":id,
            "ok":false, "error":message}),
        WorkerCommand::SetModel(id, _) => json!({"type":"set_model_reply", "session_id":id,
            "ok":false, "error":message}),
        WorkerCommand::SetPermissionMode(id, _) => json!({"type":"set_permission_mode_reply", "session_id":id,
            "ok":false, "error":message}),
        WorkerCommand::QueueList(id) => json!({"type":"queue_list_reply", "session_id":id,
            "ok":false, "error":message}),
        WorkerCommand::QueueCancel(id, queue_id) => json!({"type":"queue_cancel_reply", "session_id":id,
            "queue_id":queue_id, "ok":false, "error":message}),
        WorkerCommand::Stop(id) => json!({"type":"stop_reply", "session_id":id,
            "ok":false, "error":message}),
    }
}

pub fn run_sessions(sessions: &[Session], store: Option<crate::ui_state::UiStateStore>) -> io::Result<()> {
    let MultiBridge { frames, commands, live_ids, complete, router, workers } = connect_sessions(sessions)?;
    let result = if let Some(store) = store {
        ui::run_with_channels_state_guarded(frames, commands.clone(), store, live_ids, complete)
    } else {
        ui::run_with_channels(frames, commands.clone())
    };
    drop(commands);
    let _ = router.join();
    for worker in workers { let _ = worker.join(); }
    result
}

/// Attach to one daemon socket with the same dynamic routing used by a restored roster.
pub fn run_socket(path: impl AsRef<Path>) -> io::Result<()> {
    run_socket_expected(path, None)
}

pub fn run_socket_expected(path: impl AsRef<Path>, expected: Option<&str>) -> io::Result<()> {
    let path = path.as_ref();
    let client = DaemonClient::connect(path, None).map_err(as_io_error)?;
    let id = client.hello["session_id"].as_str()
        .filter(|id| crate::discovery::valid_id(id))
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "invalid session ID"))?
        .to_owned();
    if expected.is_some_and(|expected| expected != id) {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "fleet slot session identity changed"));
    }
    drop(client);
    run_sessions(&[Session { id, title: String::new(), socket: path.to_path_buf(), scope_key: String::new(),
        clients: None, started_at: String::new() }], None)
}

fn as_io_error(error: TransportError) -> io::Error {
    io::Error::other(error)
}

#[cfg(test)]
fn spawn_worker(client: DaemonClient) -> (Receiver<Value>, SyncSender<WorkerCommand>, JoinHandle<()>) {
    spawn_worker_with_snapshot(client, None)
}

#[cfg(test)]
fn spawn_worker_with_snapshot(client: DaemonClient, snapshot: Option<crate::transport::TranscriptSnapshot>) -> (Receiver<Value>, SyncSender<WorkerCommand>, JoinHandle<()>) {
    let (frame_tx, frame_rx) = mpsc::sync_channel(128);
    let (prompt_tx, prompt_rx) = mpsc::sync_channel(32);
    let worker = thread::spawn(move || {
        let cursor = AtomicU64::new(client.cursor);
        let stopped = AtomicBool::new(false);
        worker_loop(client, snapshot, &frame_tx, &prompt_rx, &cursor, None, &stopped);
    });
    (frame_rx, prompt_tx, worker)
}

fn worker_loop(
    mut client: DaemonClient,
    snapshot: Option<crate::transport::TranscriptSnapshot>,
    frames: &SyncSender<Value>,
    prompts: &Receiver<WorkerCommand>,
    cursor: &AtomicU64,
    roster_guard: Option<&Mutex<bool>>,
    stopped: &AtomicBool,
) {
    let session_id = client.hello["session_id"]
        .as_str()
        .unwrap_or_default()
        .to_owned();
    let live_from_seq = client.hello["next_seq"].as_u64().unwrap_or(u64::MAX);
    if frames.send(client.hello.clone()).is_err() {
        return;
    }
    if let Some(snapshot) = snapshot {
        let markdown = history::render(&snapshot);
        if !markdown.is_empty() && frames.send(json!({"type":"event", "session_id":session_id,
            "event":{"type":"text_delta", "data":{"text":markdown}}})).is_err() {
            return;
        }
    }
    loop {
        // Bound prompt work so a burst cannot starve incoming daemon events.
        for _ in 0..32 {
            match prompts.try_recv() {
                Ok(WorkerCommand::Stop(id)) => {
                    if id != session_id {
                        let _ = frames.send(json!({"type":"stop_reply", "session_id":id,
                            "ok":false, "error":"Stop target is not attached"}));
                        continue;
                    }
                    let result = client.call("stop", Map::new());
                    cursor.store(client.cursor, Ordering::Relaxed);
                    let accepted = result.as_ref().is_ok_and(|reply| reply["ok"] == true);
                    let error = match &result {
                        Ok(reply) => reply["error"].as_str().unwrap_or("Daemon refused stop").to_owned(),
                        Err(error) => error.to_string(),
                    };
                    if accepted {
                        stopped.store(true, Ordering::Release);
                        if let Some(guard) = roster_guard { revoke(guard); }
                    }
                    if frames.send(json!({"type":"stop_reply", "session_id":id,
                        "ok":accepted, "error":error})).is_err() { return; }
                    if accepted {
                        return;
                    }
                    if matches!(result, Err(TransportError::Closed)) { return; }
                }
                Ok(WorkerCommand::Launch(_, _, group)) => {
                    let _ = frames.send(json!({"type":"launch_reply", "ok":false,
                        "message":"Session launch is unavailable on this connection", "group":group}));
                }
                Ok(WorkerCommand::Attach(id, group)) => {
                    let _ = frames.send(json!({"type":"attach_reply", "ok":false,
                        "session_id":id, "message":"Session attach is unavailable on this connection", "group":group}));
                }
                Ok(WorkerCommand::Models(id)) => {
                    let result = if id == session_id { client.call("list_models", Map::new()) }
                        else { Err(TransportError::Malformed("model target is not attached")) };
                    cursor.store(client.cursor, Ordering::Relaxed);
                    let reply = match result {
                        Ok(reply) => json!({"type":"models_reply", "session_id":id,
                            "ok":reply["ok"] == true, "models":reply.get("models"),
                            "note":reply.get("note"), "loading":reply.get("loading"),
                            "error":reply.get("error")}),
                        Err(error) => json!({"type":"models_reply", "session_id":id,
                            "ok":false, "error":error.to_string()}),
                    };
                    if frames.send(reply).is_err() { return; }
                }
                Ok(WorkerCommand::SetModel(id, model)) => {
                    let result = if id == session_id {
                        let mut params = Map::new();
                        params.insert("model".into(), Value::String(model));
                        client.call("set_model", params)
                    } else { Err(TransportError::Malformed("model target is not attached")) };
                    cursor.store(client.cursor, Ordering::Relaxed);
                    let reply = match result {
                        Ok(reply) => json!({"type":"set_model_reply", "session_id":id,
                            "ok":reply["ok"] == true, "model":reply.get("model"),
                            "error":reply.get("error")}),
                        Err(error) => json!({"type":"set_model_reply", "session_id":id,
                            "ok":false, "error":error.to_string()}),
                    };
                    if frames.send(reply).is_err() { return; }
                }
                Ok(WorkerCommand::SetPermissionMode(id, mode)) => {
                    let result = if id == session_id {
                        let mut params = Map::new();
                        params.insert("mode".into(), Value::String(mode));
                        client.call("set_permission_mode", params)
                    } else { Err(TransportError::Malformed("permission target is not attached")) };
                    cursor.store(client.cursor, Ordering::Relaxed);
                    let reply = match result {
                        Ok(reply) => json!({"type":"set_permission_mode_reply", "session_id":id,
                            "ok":reply["ok"] == true, "mode":reply.get("mode"),
                            "error":reply.get("error")}),
                        Err(error) => json!({"type":"set_permission_mode_reply", "session_id":id,
                            "ok":false, "error":error.to_string()}),
                    };
                    if frames.send(reply).is_err() { return; }
                }
                Ok(WorkerCommand::QueueList(id)) => {
                    let result = if id == session_id { client.call("queue", Map::new()) }
                        else { Err(TransportError::Malformed("queue target is not attached")) };
                    cursor.store(client.cursor, Ordering::Relaxed);
                    if matches!(result, Err(TransportError::Closed)) {
                        if let Some(guard) = roster_guard { revoke(guard); }
                    }
                    let frame = match &result {
                        Ok(reply) if reply["ok"] == true => json!({"type":"queue_list_reply",
                            "session_id":id,"ok":true,"rows":safe_queue_rows(reply)}),
                        Ok(reply) => json!({"type":"queue_list_reply","session_id":id,
                            "ok":false,"error":reply["error"].as_str().unwrap_or("Queue unavailable")}),
                        Err(error) => json!({"type":"queue_list_reply","session_id":id,
                            "ok":false,"error":error.to_string()}),
                    };
                    if frames.send(frame).is_err() { return; }
                    if matches!(result, Err(TransportError::Closed)) { return; }
                }
                Ok(WorkerCommand::QueueCancel(id, queue_id)) => {
                    let result = if id == session_id {
                        let mut params = Map::new();
                        params.insert("id".into(), Value::String(queue_id.clone()));
                        client.call("cancel_queued", params)
                    } else { Err(TransportError::Malformed("queue target is not attached")) };
                    cursor.store(client.cursor, Ordering::Relaxed);
                    if matches!(result, Err(TransportError::Closed)) {
                        if let Some(guard) = roster_guard { revoke(guard); }
                    }
                    let frame = match &result {
                        Ok(reply) => json!({"type":"queue_cancel_reply","session_id":id,
                            "queue_id":queue_id,"ok":reply["ok"] == true,
                            "error":reply.get("error")}),
                        Err(error) => json!({"type":"queue_cancel_reply","session_id":id,
                            "queue_id":queue_id,"ok":false,"error":error.to_string()}),
                    };
                    if frames.send(frame).is_err() { return; }
                    if matches!(result, Err(TransportError::Closed)) { return; }
                }
                Ok(WorkerCommand::Answer(session, id, answer)) => {
                    if session != session_id || !answer.is_object() {
                        let _ = frames.send(json!({"type":"answer_reply", "session_id":session,
                            "request_id":id, "ok":false, "message":"Invalid answer target or payload"}));
                        continue;
                    }
                    let mut params = Map::new();
                    params.insert("id".into(), Value::String(id.clone()));
                    params.insert("answer".into(), answer);
                    let result = client.call("answer_needs_input", params);
                    cursor.store(client.cursor, Ordering::Relaxed);
                    if matches!(result, Err(TransportError::Closed)) {
                        if let Some(guard) = roster_guard { revoke(guard); }
                    }
                    let frame = match result {
                        Ok(ref reply) => json!({"type":"answer_reply", "session_id":session,
                            "request_id":id, "ok":reply["ok"] == true,
                            "message":reply["error"].as_str().unwrap_or("Request no longer pending")}),
                        Err(ref error) => json!({"type":"answer_reply", "session_id":session,
                            "request_id":id, "ok":false,
                            "uncertain":!matches!(error, TransportError::FrameTooLarge | TransportError::RequestIdsExhausted),
                            "message":error.to_string()}),
                    };
                    if frames.send(frame).is_err() {
                        return;
                    }
                    if matches!(result, Err(TransportError::Closed)) {
                        return;
                    }
                }
                Ok(WorkerCommand::Peers(id)) => {
                    if id != session_id {
                        let _ = frames.send(json!({"type":"peer_roster", "session_id":id,
                            "ok":false, "error":"Peer target is not attached"}));
                        continue;
                    }
                    let result = client.call("peers", Map::new());
                    cursor.store(client.cursor, Ordering::Relaxed);
                    if matches!(result, Err(TransportError::Closed)) {
                        if let Some(guard) = roster_guard { revoke(guard); }
                    }
                    let reply = match result {
                        Ok(ref reply) => json!({"type":"peer_roster", "session_id":id,
                            "ok":reply["ok"] == true, "peers":reply.get("peers"),
                            "error":reply.get("error")}),
                        Err(ref error) => json!({"type":"peer_roster", "session_id":id,
                            "ok":false, "error":error.to_string()}),
                    };
                    if frames.send(reply).is_err() { return; }
                    if matches!(result, Err(TransportError::Closed)) { return; }
                }
                Ok(WorkerCommand::Message(id, target, text)) => {
                    let draft = format!("/msg {target} {text}");
                    if id != session_id {
                        let _ = frames.send(json!({"type":"peer_message_reply", "session_id":id,
                            "ok":false, "error":"Peer target session is not attached", "draft":draft}));
                        continue;
                    }
                    let mut params = Map::new();
                    params.insert("target".into(), Value::String(target));
                    params.insert("text".into(), Value::String(text));
                    let result = client.call("msg", params);
                    cursor.store(client.cursor, Ordering::Relaxed);
                    if matches!(result, Err(TransportError::Closed)) {
                        if let Some(guard) = roster_guard { revoke(guard); }
                    }
                    let reply = match &result {
                        Ok(reply) => json!({"type":"peer_message_reply", "session_id":id,
                            "ok":reply["ok"] == true, "peer":reply.get("peer"),
                            "delivered_to":reply.get("delivered_to"),
                            "failed":reply.get("failed"), "ledger_error":reply.get("ledger_error"),
                            "error":reply.get("error"), "draft":draft}),
                        Err(error) => json!({"type":"peer_message_reply", "session_id":id,
                            "ok":false,
                            "uncertain":!matches!(error, TransportError::FrameTooLarge | TransportError::RequestIdsExhausted),
                            "error":error.to_string(), "draft":draft}),
                    };
                    if frames.send(reply).is_err() { return; }
                    if matches!(result, Err(TransportError::Closed)) { return; }
                }
                Ok(WorkerCommand::Prompt(id, text)) => {
                    if id != session_id {
                        if frames
                            .send(json!({"type":"prompt_rejected", "session_id":id, "text":text,
                            "message":"Prompt target is not attached"}))
                            .is_err()
                        {
                            return;
                        }
                        continue;
                    }
                    match client.prompt(&text) {
                        Ok(reply) => {
                            let mut frame = if reply["ok"] == false {
                                json!({"type":"prompt_rejected", "session_id":id, "text":text,
                                    "message":reply["error"].as_str().unwrap_or("Prompt refused")})
                            } else {
                                reply
                            };
                            let rejected = frame["type"] == "prompt_rejected";
                            frame["session_id"] = Value::String(session_id.clone());
                            if frames.send(frame).is_err() {
                                return;
                            }
                            if rejected && !forward_status(&mut client, frames, &session_id) {
                                return;
                            }
                        }
                        Err(error) => {
                            if matches!(error, TransportError::Closed) {
                                if let Some(guard) = roster_guard { revoke(guard); }
                            }
                            // Once bytes may have been written, a missing reply does not
                            // prove that the daemon rejected the prompt. Keep the draft
                            // available, but require a deliberate retry by the user.
                            let (kind, message) = match error {
                                TransportError::FrameTooLarge
                                | TransportError::RequestIdsExhausted => {
                                    ("prompt_rejected", "Prompt could not be sent")
                                }
                                TransportError::Timeout => {
                                    ("prompt_uncertain", "Prompt delivery unconfirmed")
                                }
                                TransportError::Closed => (
                                    "prompt_uncertain",
                                    "Daemon disconnected; prompt delivery unconfirmed",
                                ),
                                _ => ("prompt_uncertain", "Prompt delivery unconfirmed"),
                            };
                            let _ =
                                frames.send(json!({"type":kind, "session_id":session_id,
                                    "text":text, "message":message}));
                            if matches!(error, TransportError::Closed) {
                                return;
                            }
                        }
                    }
                    cursor.store(client.cursor, Ordering::Relaxed);
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => return,
            }
        }
        match client.poll_frame(Duration::from_millis(50)) {
            Ok(Some(mut frame)) => {
                let terminal = frame["type"] == "event"
                    && matches!(frame["event"]["type"].as_str(), Some("turn_done" | "turn_refused"))
                    && frame["seq"].as_u64().is_some_and(|seq| seq >= live_from_seq);
                frame["session_id"] = Value::String(session_id.clone());
                if frames.send(frame).is_err() {
                    return;
                }
                if terminal && !forward_status(&mut client, frames, &session_id) { return; }
            }
            Ok(None) => {}
            Err(error) => {
                if let Some(guard) = roster_guard { revoke(guard); }
                let message = match error {
                    TransportError::Closed => "Daemon disconnected",
                    TransportError::FrameTooLarge => "Daemon sent an oversized frame",
                    _ => "Daemon connection failed",
                };
                let _ = frames.send(json!({"type":"client_notice", "session_id":session_id,
                    "message":message}));
                return;
            }
        }
        cursor.store(client.cursor, Ordering::Relaxed);
    }
}

fn forward_status(client: &mut DaemonClient, frames: &SyncSender<Value>, session_id: &str) -> bool {
    match client.call("status", Map::new()) {
        Ok(mut reply) if reply["ok"] == true && reply.get("status").is_some() => {
            // The runtime may publish turn_done before clearing its busy flag.
            // Keep this internal refresh separate from a user-requested status
            // reply so it cannot restore stale activity or replace the notice.
            reply["type"] = Value::String("telemetry_status".into());
            reply["session_id"] = Value::String(session_id.to_owned());
            if let Some(status) = reply.get_mut("status").and_then(Value::as_object_mut) {
                status.insert("session_id".into(), Value::String(session_id.to_owned()));
            }
            frames.send(reply).is_ok()
        }
        Err(TransportError::Closed) => false,
        _ => frames.send(json!({"type":"telemetry_unavailable", "session_id":session_id})).is_ok(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Write};
    use std::os::unix::net::UnixListener;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static NEXT_SOCKET: AtomicUsize = AtomicUsize::new(0);

    #[test]
    fn queue_rows_scrub_raw_python_prompts_before_ui_delivery() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("fake-lore");
        std::fs::write(&script, r#"#!/usr/bin/env python3
import json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    text = req.get('text', '').replace('SECRET', '[redacted]')
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'text':text}), flush=True)
"#).unwrap();
        let mut perms = std::fs::metadata(&script).unwrap().permissions();
        perms.set_mode(0o700);
        std::fs::set_permissions(&script, perms).unwrap();
        let rows = safe_queue_rows_with_python(&json!({"queue":[
            {"id":"q7","text":"my SECRET token"}, {"id":"../unsafe","text":"SECRET"}
        ]}), &script);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["id"], "q7");
        assert_eq!(rows[0]["preview"], "my [redacted] token");
        assert!(!serde_json::to_string(&rows).unwrap().contains("SECRET"));
        let unavailable = safe_queue_rows_with_python(&json!({"queue":[{"id":"q8","text":"SECRET"}]}),
            &dir.path().join("missing-interpreter"));
        assert_eq!(unavailable[0]["preview"], "[preview unavailable]");
    }

    #[test]
    fn live_daemon_frames_and_prompts_cross_the_ui_bridge() {
        let path = std::env::temp_dir().join(format!(
            "doxa-rust-bridge-{}-{}.sock",
            std::process::id(),
            NEXT_SOCKET.fetch_add(1, Ordering::Relaxed)
        ));
        let listener = UnixListener::bind(&path).unwrap();
        let server = thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            writeln!(
                socket,
                "{}",
                json!({
                    "type":"hello", "proto":1, "session_id":"session-1",
                    "cwd":"/tmp", "model":"test", "engine":"claude", "next_seq":1
                })
            )
            .unwrap();
            let mut reader = BufReader::new(socket.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            assert_eq!(
                serde_json::from_str::<Value>(&line).unwrap(),
                json!({"type":"attach","cursor":null})
            );
            writeln!(
                socket,
                "{}",
                json!({
                    "type":"event", "seq":1, "turn":null,
                    "event":{"type":"text_delta", "data":{"text":"hello"}}
                })
            )
            .unwrap();
            line.clear();
            reader.read_line(&mut line).unwrap();
            assert_eq!(
                serde_json::from_str::<Value>(&line).unwrap(),
                json!({"type":"prompt","id":1,"text":"next"})
            );
            writeln!(
                socket,
                "{}",
                json!({"type":"reply","id":1,"ok":true,"turn":"turn-1"})
            )
            .unwrap();
            line.clear();
            reader.read_line(&mut line).unwrap();
            assert_eq!(serde_json::from_str::<Value>(&line).unwrap(),
                json!({"type":"call","id":2,"method":"peers","params":{}}));
            writeln!(socket, "{}", json!({"type":"reply","id":2,"ok":true,
                "peers":[{"session_id":"peer-1","title":"Builder"}]})).unwrap();
            line.clear();
            reader.read_line(&mut line).unwrap();
            assert_eq!(serde_json::from_str::<Value>(&line).unwrap(),
                json!({"type":"call","id":3,"method":"msg",
                    "params":{"target":"peer-1","text":"hello"}}));
            writeln!(socket, "{}", json!({"type":"reply","id":3,"ok":true,
                "peer":{"session_id":"peer-1","title":"Builder"},
                "delivered_to":["peer-1"],"failed":[]})).unwrap();
        });
        let client = DaemonClient::connect(&path, None).unwrap();
        let (frames, prompts, worker) = spawn_worker(client);
        assert_eq!(
            frames.recv_timeout(Duration::from_secs(2)).unwrap()["session_id"],
            "session-1"
        );
        let event = frames.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(event["session_id"], "session-1");
        assert_eq!(event["event"]["data"]["text"], "hello");
        prompts
            .send(WorkerCommand::Prompt("session-1".into(), "next".into()))
            .unwrap();
        assert_eq!(
            frames.recv_timeout(Duration::from_secs(2)).unwrap()["turn"],
            "turn-1"
        );
        prompts.send(WorkerCommand::Peers("session-1".into())).unwrap();
        let peers = frames.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(peers["type"], "peer_roster");
        assert_eq!(peers["peers"][0]["session_id"], "peer-1");
        prompts.send(WorkerCommand::Message("session-1".into(), "peer-1".into(), "hello".into())).unwrap();
        let sent = frames.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(sent["type"], "peer_message_reply");
        assert_eq!(sent["delivered_to"], json!(["peer-1"]));
        drop(prompts);
        worker.join().unwrap();
        server.join().unwrap();
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn live_turn_completion_refreshes_lore_status() {
        let path = std::env::temp_dir().join(format!("doxa-rust-status-{}-{}.sock",
            std::process::id(), NEXT_SOCKET.fetch_add(1, Ordering::Relaxed)));
        let listener = UnixListener::bind(&path).unwrap();
        let server = thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            writeln!(socket, "{}", json!({"type":"hello", "proto":1,
                "session_id":"session-1", "engine":"codex", "cwd":"/tmp",
                "next_seq":1, "lore_scrub":"ready"})).unwrap();
            let mut reader = BufReader::new(socket.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap(); // attach
            writeln!(socket, "{}", json!({"type":"event", "seq":1, "turn":"turn-1",
                "event":{"type":"turn_done", "data":{"is_error":true}}})).unwrap();
            line.clear();
            reader.read_line(&mut line).unwrap();
            let call: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(call["method"], "status");
            writeln!(socket, "{}", json!({"type":"reply", "id":call["id"], "ok":true,
                "status":{"session_id":"session-1", "lore_scrub":"unavailable"}})).unwrap();
        });
        let client = DaemonClient::connect(&path, None).unwrap();
        let (frames, prompts, worker) = spawn_worker(client);
        assert_eq!(frames.recv_timeout(Duration::from_secs(2)).unwrap()["lore_scrub"], "ready");
        assert_eq!(frames.recv_timeout(Duration::from_secs(2)).unwrap()["event"]["type"], "turn_done");
        let status = frames.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(status["type"], "telemetry_status");
        assert_eq!(status["status"]["lore_scrub"], "unavailable");
        drop(prompts);
        worker.join().unwrap();
        server.join().unwrap();
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn daemon_rejection_returns_the_original_prompt_to_ui() {
        let path = std::env::temp_dir().join(format!(
            "doxa-rust-bridge-reject-{}-{}.sock",
            std::process::id(),
            NEXT_SOCKET.fetch_add(1, Ordering::Relaxed)
        ));
        let listener = UnixListener::bind(&path).unwrap();
        let server = thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            writeln!(
                socket,
                "{}",
                json!({
                    "type":"hello", "proto":1, "session_id":"session-1",
                    "cwd":"/tmp", "model":"test", "engine":"claude", "next_seq":0
                })
            )
            .unwrap();
            let mut reader = BufReader::new(socket.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap(); // attach
            line.clear();
            reader.read_line(&mut line).unwrap();
            let request: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(request["text"], "please retry");
            writeln!(
                socket,
                "{}",
                json!({"type":"reply", "id":request["id"], "ok":false, "error":"queue full"})
            )
            .unwrap();
        });
        let client = DaemonClient::connect(&path, None).unwrap();
        let (frames, prompts, worker) = spawn_worker(client);
        frames.recv_timeout(Duration::from_secs(2)).unwrap(); // hello
        prompts
            .send(WorkerCommand::Prompt(
                "session-1".into(),
                "please retry".into(),
            ))
            .unwrap();
        let rejected = frames.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(rejected["type"], "prompt_rejected");
        assert_eq!(rejected["text"], "please retry");
        assert_eq!(rejected["message"], "queue full");
        drop(prompts);
        worker.join().unwrap();
        server.join().unwrap();
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn answer_uses_call_protocol_and_reports_reply() {
        let path = std::env::temp_dir().join(format!(
            "doxa-rust-answer-{}-{}.sock",
            std::process::id(),
            NEXT_SOCKET.fetch_add(1, Ordering::Relaxed)
        ));
        let listener = UnixListener::bind(&path).unwrap();
        let server = thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            writeln!(
                socket,
                "{}",
                json!({"type":"hello", "proto":1, "session_id":"session-1",
                "cwd":"/tmp", "model":"test", "engine":"claude", "next_seq":0})
            )
            .unwrap();
            let mut reader = BufReader::new(socket.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap(); // attach
            line.clear();
            reader.read_line(&mut line).unwrap();
            let call: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(call["type"], "call");
            assert_eq!(call["method"], "answer_needs_input");
            assert_eq!(
                call["params"],
                json!({"id":"req-1", "answer":{"decision":"deny"}})
            );
            writeln!(
                socket,
                "{}",
                json!({"type":"reply", "id":call["id"], "ok":true})
            )
            .unwrap();
        });
        let client = DaemonClient::connect(&path, None).unwrap();
        let (frames, commands, worker) = spawn_worker(client);
        frames.recv_timeout(Duration::from_secs(2)).unwrap();
        commands
            .send(WorkerCommand::Answer(
                "session-1".into(),
                "req-1".into(),
                json!({"decision":"deny"}),
            ))
            .unwrap();
        let reply = frames.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(reply["type"], "answer_reply");
        assert_eq!(reply["ok"], true);
        drop(commands);
        worker.join().unwrap();
        server.join().unwrap();
        std::fs::remove_file(path).unwrap();
    }
}

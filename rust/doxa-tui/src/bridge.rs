//! Connect one daemon socket to the terminal loop without blocking input.

use std::collections::{HashMap, HashSet};
use std::io;
use std::fs;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::mpsc::{self, Receiver, SyncSender, TryRecvError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde_json::{Map, Value};
#[cfg(test)]
use serde_json::json;

use crate::transport::{DaemonClient, TransportError};
use crate::history;
use crate::ui;
use crate::worker_frames::{WorkerFrame, LaunchResult, PromptDelivery, CommandResult, ReplyStatus, QueueRow, wire_string, wire_value};
use crate::discovery::Session;
use crate::launch::{self, LaunchOptions};

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
    SetEffort(String, String),
    SetPermissionMode(String, String),
    SetIsolation(String, String),
    Branch(String, Option<String>),
    QueueList(String),
    ContextDetail(String),
    RemoteHistory(String, u64),
    QueueCancel(String, String),
    Status(String),
    Stop(String),
    FinalizeForClear(String),
}

fn safe_queue_rows(reply: &Value) -> Vec<QueueRow> {
    safe_queue_rows_with_client(reply, doxa_lore::LoreClient::open(Duration::from_secs(2)).ok())
}

fn safe_queue_rows_with_client(reply: &Value, mut lore: Option<doxa_lore::LoreClient>) -> Vec<QueueRow> {
    let Some(rows) = reply["queue"].as_array() else { return Vec::new(); };
    rows.iter().take(64).filter_map(|row| {
        let id = row["id"].as_str()?;
        if id.is_empty() || id.len() > 128 || !id.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-') {
            return None;
        }
        let preview = row["text"].as_str().filter(|text| text.len() <= 64 * 1024)
            .and_then(|text| lore.as_mut()?.scrub(text).ok())
            .map(|text| text.chars().take(160).collect::<String>())
            .unwrap_or_else(|| "[preview unavailable]".into());
        Some(QueueRow { id: id.to_owned(), preview })
    }).collect()
}

fn attach_worker(session: &Session, frames: &SyncSender<WorkerFrame>, guard: &Arc<Mutex<bool>>)
    -> io::Result<(SyncSender<WorkerCommand>, Arc<AtomicBool>, JoinHandle<()>)> {
    let (mut client, snapshot) = DaemonClient::connect_for_restore(&session.socket).map_err(io::Error::other)?;
    if client.hello["session_id"] != session.id {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "session identity changed during attach"));
    }
    if !session.title.is_empty() { client.hello["title"] = Value::String(session.title.clone()); }
    let (tx, rx) = mpsc::sync_channel(32);
    let connected = Arc::new(AtomicBool::new(true));
    let connected_worker = Arc::clone(&connected);
    let path = session.socket.clone();
    let id = session.id.clone();
    let title = session.title.clone();
    let frames = frames.clone();
    let guard = Arc::clone(guard);
    let worker = thread::spawn(move || {
        let cursor = AtomicU64::new(client.cursor);
        let stopped = AtomicBool::new(false);
        let cleared = AtomicBool::new(false);
        let mut current = (client, snapshot);
        loop {
            worker_loop(current.0, current.1, &frames, &rx, &cursor, Some(&guard), &stopped, &cleared);
            connected_worker.store(false, Ordering::Release);
            if stopped.load(Ordering::Acquire) {
                if !cleared.load(Ordering::Acquire) { revoke(&guard); }
                while let Ok(command) = rx.try_recv() {
                    let _ = frames.send(rejection_frame(command, "Session is stopping"));
                }
                return;
            }
            revoke(&guard);
            if frames.send(WorkerFrame::Notice { session_id: id.clone(), message: "Daemon disconnected; reconnecting".into() }).is_err() { return; }
            loop {
                match rx.recv_timeout(Duration::from_millis(250)) {
                    Err(mpsc::RecvTimeoutError::Disconnected) => return,
                    Ok(command) => { let _ = frames.send(rejection_frame(command, "Daemon reconnecting")); }
                    Err(mpsc::RecvTimeoutError::Timeout) => {}
                }
                let runtime=path.parent().unwrap_or(Path::new("/"));
                let reconnect=crate::discovery::sessions_in(runtime).ok().and_then(|sessions|sessions.into_iter().find(|session|session.id==id)).map(|session|session.socket).unwrap_or_else(||path.clone());
                let replay=if reconnect==path{Some(cursor.load(Ordering::Relaxed))}else{None};
                match DaemonClient::connect(&reconnect, replay) {
                    Ok(mut client) if client.hello["session_id"] == id => {
                        if !title.is_empty() { client.hello["title"] = Value::String(title.clone()); }
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
    pub frames: Receiver<WorkerFrame>,
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

pub fn connect_sessions(sessions: &[Session]) -> io::Result<MultiBridge> { connect_sessions_inner(sessions,false) }

pub(crate) fn connect_sessions_inner(sessions:&[Session],readonly_restore:bool)->io::Result<MultiBridge> {
    if (sessions.is_empty() && !readonly_restore) || sessions.len() > crate::startup_restore::MAX_STARTUP_TABS {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "expected 1–257 startup sessions"));
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
                let _ = frame_tx.try_send(WorkerFrame::Notice { session_id: session.id.clone(), message: "Session unavailable; saved layout is read-only".into() });
                continue;
            }
        };
        routes.insert(session.id.clone(), (tx, Arc::clone(&connected)));
        live_ids.push(session.id.clone());
        workers.push(worker);
    }
    if live_ids.is_empty() && !readonly_restore {
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
                        let _ = router_frames.send(WorkerFrame::Launch { group, result: LaunchResult::Attached { session_id: id.clone() } });
                        if let Some(prompt) = prompt {
                            let _ = tx.send(WorkerCommand::Prompt(id, prompt));
                        }
                    }
                    Err(error) => { let _ = router_frames.send(WorkerFrame::Launch { group, result: LaunchResult::Failed {
                        message: format!("UI attach failed ({error}); use doxa-rs attach {}", session.id),
                        started_session: Some(session.id) } }); }
                    },
                    Err(error) => { let _ = router_frames.send(WorkerFrame::Launch { group, result: LaunchResult::Failed { message: error.to_string(), started_session: None } }); }
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
                        let _ = router_frames.send(WorkerFrame::Attach { session_id: id, group, result: Ok(()) });
                        continue;
                    }
                    if routes.len() >= crate::ui::panes::MAX_TABS {
                        let _ = router_frames.send(WorkerFrame::Attach { session_id: id, group, result: Err("256 attached sessions is the limit".into()) });
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
                            let _ = router_frames.send(WorkerFrame::Attach { session_id: id, group, result: Ok(()) });
                        }
                        Err(error) => { let _ = router_frames.send(WorkerFrame::Attach { session_id: id, group, result: Err(error.to_string()) }); }
                    }
                    continue;
                }
                WorkerCommand::Launch(options, prompt, group) => {
                    if routes.len().saturating_add(launches_in_flight) >= crate::ui::panes::MAX_TABS {
                        let _ = router_frames.send(WorkerFrame::Launch { group, result: LaunchResult::Failed { message: "256 attached sessions is the limit".into(), started_session: None } });
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
                | WorkerCommand::Models(id) | WorkerCommand::SetModel(id, _) | WorkerCommand::SetEffort(id, _)
                | WorkerCommand::SetPermissionMode(id, _) | WorkerCommand::SetIsolation(id, _) | WorkerCommand::QueueList(id) | WorkerCommand::ContextDetail(id) | WorkerCommand::Status(id)
                | WorkerCommand::Branch(id, _)
                | WorkerCommand::QueueCancel(id, _) | WorkerCommand::RemoteHistory(id,_) => id,
                WorkerCommand::Message(id, _, _) | WorkerCommand::Stop(id)
                | WorkerCommand::FinalizeForClear(id) => id,
                WorkerCommand::Launch(_, _, _) | WorkerCommand::Attach(_, _) => unreachable!(),
            };
            let Some((route, connected)) = routes.get(id) else {
                let _ = router_frames.send(rejection_frame(command, "Session is not attached"));
                continue;
            };
            if !connected.load(Ordering::Acquire) {
                let _ = router_frames.send(rejection_frame(command, "Daemon reconnecting"));
                continue;
            }
            match route.try_send(command) {
                Ok(()) => {}
                Err(mpsc::TrySendError::Full(command)) => {
                    let _ = router_frames.send(rejection_frame(command, "Daemon command queue full"));
                }
                Err(mpsc::TrySendError::Disconnected(command)) => {
                    let _ = router_frames.send(rejection_frame(command, "Daemon worker unavailable"));
                }
            }
        }
        drop(routes);
        for worker in added_workers { let _ = worker.join(); }
    });
    drop(frame_tx);
    Ok(MultiBridge { frames: frame_rx, commands: command_tx, live_ids, complete, router, workers })
}

fn command_frame(session_id: String, result: CommandResult) -> WorkerFrame {
    WorkerFrame::Command { session_id, result }
}

pub(crate) fn rejection_frame(command: WorkerCommand, message: &str) -> WorkerFrame {
    let status = ReplyStatus::failed(message);
    match command {
        WorkerCommand::Launch(_, _, group) => WorkerFrame::Launch { group,
            result: LaunchResult::Failed { message: message.into(), started_session: None } },
        WorkerCommand::Attach(session_id, group) => WorkerFrame::Attach { session_id, group, result: Err(message.into()) },
        WorkerCommand::Prompt(session_id, text) => WorkerFrame::PromptFailed { session_id, text,
            message: message.into(), delivery: PromptDelivery::Rejected },
        WorkerCommand::Answer(session_id, request_id, _) => command_frame(session_id,
            CommandResult::Answer { request_id, ok: false, uncertain: None, message: message.into() }),
        WorkerCommand::Peers(id) => command_frame(id, CommandResult::PeerRoster { status, peers: None }),
        WorkerCommand::Message(id, target, text) => command_frame(id, CommandResult::PeerMessage { status,
            uncertain: Some(false), draft: format!("/msg {target} {text}"), peer: None, delivered_to: None, failed: None, ledger_error: None }),
        WorkerCommand::Models(id) => command_frame(id, CommandResult::Models { status, models: None,
            note: None, loading: None, capabilities: None }),
        WorkerCommand::ContextDetail(id) => command_frame(id, CommandResult::ContextDetail { status, detail: None }),
        WorkerCommand::RemoteHistory(session_id,_) => WorkerFrame::RemoteHistoryPage {
            session_id,markdown:String::new(),before:None,has_more:false,error:Some(message.into()) },
        WorkerCommand::SetModel(id, _) => command_frame(id, CommandResult::SetModel { status, model: None }),
        WorkerCommand::SetEffort(id, _) => command_frame(id, CommandResult::SetEffort { status, effort: None, verification_pending: None }),
        WorkerCommand::SetPermissionMode(id, _) => command_frame(id, CommandResult::SetPermissionMode { status, mode: None }),
        WorkerCommand::SetIsolation(id, _) => command_frame(id, CommandResult::SetIsolation { status, isolation: None }),
        WorkerCommand::Branch(id, _) => command_frame(id, CommandResult::Branch { status, base: None, branches: None, message: None }),
        WorkerCommand::QueueList(id) => command_frame(id, CommandResult::QueueList { status, rows: Vec::new() }),
        WorkerCommand::Status(session_id) => WorkerFrame::TelemetryUnavailable { session_id },
        WorkerCommand::QueueCancel(id, queue_id) => command_frame(id, CommandResult::QueueCancel { status, queue_id }),
        WorkerCommand::Stop(id) => command_frame(id, CommandResult::Stop { status, for_clear: false }),
        WorkerCommand::FinalizeForClear(id) => command_frame(id, CommandResult::Stop { status, for_clear: true }),
    }
}

pub fn run_sessions(sessions: &[Session], store: Option<crate::ui_state::UiStateStore>) -> io::Result<()> {
    let MultiBridge { frames, commands, live_ids, complete, router, workers } = connect_sessions_inner(sessions,store.as_ref().is_some_and(|store|!store.startup_archives.is_empty() || !store.startup_notice.is_empty()))?;
    let result = if let Some(store) = store {
        ui::run_with_worker_channels_state_guarded(frames, commands.clone(), store, live_ids, complete)
    } else {
        ui::run_with_worker_channels(frames, commands.clone())
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
    let path = fs::canonicalize(path.as_ref())?;
    let client = DaemonClient::connect(&path, None).map_err(as_io_error)?;
    let id = client.hello["session_id"].as_str()
        .filter(|id| crate::discovery::valid_id(id))
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "invalid session ID"))?
        .to_owned();
    if expected.is_some_and(|expected| expected != id) {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "fleet slot session identity changed"));
    }
    drop(client);
    let runtime = path.parent().ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput,
        "daemon socket has no runtime directory"))?;
    let session = match registered_socket(crate::discovery::sessions_in(runtime)?, &id, &path) {
        Ok(session) => session,
        // Explicit socket attachment worked before titles lived in the
        // registry. Retain it when an otherwise valid daemon has no entry.
        Err(error) if error.kind() == io::ErrorKind::NotFound => Session {
            id,
            title: String::new(),
            socket: path,
            scope_key: String::new(),
            clients: None,
            started_at: String::new(),
        },
        Err(error) => return Err(error),
    };
    run_sessions(&[session], None)
}

fn registered_socket(sessions: Vec<Session>, id: &str, path: &Path) -> io::Result<Session> {
    sessions.into_iter().find(|session| session.id == id && session.socket == path)
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "session is not in the trusted live registry"))
}

fn as_io_error(error: TransportError) -> io::Error {
    io::Error::other(error)
}

#[cfg(test)]
fn spawn_worker(client: DaemonClient) -> (Receiver<WorkerFrame>, SyncSender<WorkerCommand>, JoinHandle<()>) {
    spawn_worker_with_snapshot(client, None)
}

#[cfg(test)]
fn spawn_worker_with_snapshot(client: DaemonClient, snapshot: Option<crate::transport::TranscriptSnapshot>) -> (Receiver<WorkerFrame>, SyncSender<WorkerCommand>, JoinHandle<()>) {
    let (frame_tx, frame_rx) = mpsc::sync_channel(128);
    let (prompt_tx, prompt_rx) = mpsc::sync_channel(32);
    let worker = thread::spawn(move || {
        let cursor = AtomicU64::new(client.cursor);
        let stopped = AtomicBool::new(false);
        let cleared = AtomicBool::new(false);
        worker_loop(client, snapshot, &frame_tx, &prompt_rx, &cursor, None, &stopped, &cleared);
    });
    (frame_rx, prompt_tx, worker)
}

fn worker_loop(
    mut client: DaemonClient,
    snapshot: Option<crate::transport::TranscriptSnapshot>,
    frames: &SyncSender<WorkerFrame>,
    prompts: &Receiver<WorkerCommand>,
    cursor: &AtomicU64,
    roster_guard: Option<&Mutex<bool>>,
    stopped: &AtomicBool,
    cleared: &AtomicBool,
) {
    let session_id = client.hello["session_id"]
        .as_str()
        .unwrap_or_default()
        .to_owned();
    let mut live_from_seq = client.hello["next_seq"].as_u64().unwrap_or(u64::MAX);
    if frames.send(WorkerFrame::Daemon { session_id: session_id.clone(), frame: client.hello.clone() }).is_err() {
        return;
    }
    if let Some(snapshot) = snapshot {
        let markdown = history::render(&snapshot);
        if !markdown.is_empty() && frames.send(WorkerFrame::SnapshotText { session_id: session_id.clone(), markdown }).is_err() {
            return;
        }
    }
    let deepseek = client.hello["engine"] == "deepseek";
    let mut balance_checked = Instant::now();
    if deepseek && !forward_status(&mut client, frames, &session_id) { return; }
    loop {
        // Bound prompt work so a burst cannot starve incoming daemon events.
        for _ in 0..32 {
            match prompts.try_recv() {
                Ok(command @ (WorkerCommand::Stop(_) | WorkerCommand::FinalizeForClear(_))) => {
                    let (id, for_clear) = match command {
                        WorkerCommand::Stop(id) => (id, false),
                        WorkerCommand::FinalizeForClear(id) => (id, true),
                        _ => unreachable!(),
                    };
                    if id != session_id {
                        let _ = frames.send(command_frame(id, CommandResult::Stop { for_clear, status: ReplyStatus::failed("Stop target is not attached") }));
                        continue;
                    }
                    let result = client.call(if for_clear { "stop_if_idle" } else { "stop" }, Map::new());
                    cursor.store(client.cursor, Ordering::Relaxed);
                    let accepted = result.as_ref().is_ok_and(|reply| reply["ok"] == true);
                    let error = match &result {
                        Ok(reply) => reply["error"].as_str().unwrap_or("Daemon refused stop").to_owned(),
                        Err(error) => error.to_string(),
                    };
                    if accepted {
                        stopped.store(true, Ordering::Release);
                        if for_clear { cleared.store(true, Ordering::Release); }
                        else if let Some(guard) = roster_guard { revoke(guard); }
                    }
                    if frames.send(command_frame(id, CommandResult::Stop { for_clear, status: ReplyStatus { ok: accepted, error: Some(error) } })).is_err() { return; }
                    if accepted {
                        return;
                    }
                    if matches!(result, Err(TransportError::Closed)) { return; }
                }
                Ok(WorkerCommand::Status(id)) => {
                    if id == session_id && !forward_status(&mut client, frames, &session_id) { return; }
                    cursor.store(client.cursor, Ordering::Relaxed);
                }
                Ok(WorkerCommand::RemoteHistory(id,_)) => {
                    if frames.send(rejection_frame(WorkerCommand::RemoteHistory(id,0),"History paging is remote-only")).is_err(){return;}
                }
                Ok(WorkerCommand::Launch(_, _, group)) => {
                    let _ = frames.send(WorkerFrame::Launch { group, result: LaunchResult::Failed { message: "Session launch is unavailable on this connection".into(), started_session: None } });
                }
                Ok(WorkerCommand::Attach(id, group)) => {
                    let _ = frames.send(WorkerFrame::Attach { session_id: id, group, result: Err("Session attach is unavailable on this connection".into()) });
                }
                Ok(WorkerCommand::Models(id)) => {
                    let result = if id == session_id { client.call("list_models", Map::new()) }
                        else { Err(TransportError::Malformed("model target is not attached")) };
                    cursor.store(client.cursor, Ordering::Relaxed);
                    let reply = match result {
                        Ok(reply) => CommandResult::Models { status: ReplyStatus::from_wire(&reply),
                            models: wire_value(&reply, "models"), note: wire_string(&reply, "note"),
                            loading: reply.get("loading").and_then(Value::as_bool), capabilities: wire_value(&reply, "capabilities") },
                        Err(error) => CommandResult::Models { status: ReplyStatus::failed(error.to_string()),
                            models: None, note: None, loading: None, capabilities: None },
                    };
                    if frames.send(command_frame(id, reply)).is_err() { return; }
                    if deepseek && !forward_status(&mut client, frames, &session_id) { return; }
                }
                Ok(WorkerCommand::SetModel(id, model)) => {
                    let result = if id == session_id {
                        let mut params = Map::new();
                        params.insert("model".into(), Value::String(model));
                        client.call("set_model", params)
                    } else { Err(TransportError::Malformed("model target is not attached")) };
                    cursor.store(client.cursor, Ordering::Relaxed);
                    let reply = match result {
                        Ok(reply) => CommandResult::SetModel { status: ReplyStatus::from_wire(&reply), model: wire_string(&reply, "model") },
                        Err(error) => CommandResult::SetModel { status: ReplyStatus::failed(error.to_string()), model: None },
                    };
                    if frames.send(command_frame(id, reply)).is_err() { return; }
                }
                Ok(WorkerCommand::SetEffort(id, effort)) => {
                    let result = if id == session_id {
                        let mut params = Map::new();
                        params.insert("effort".into(), Value::String(effort));
                        client.call("set_effort", params)
                    } else { Err(TransportError::Malformed("model target is not attached")) };
                    cursor.store(client.cursor, Ordering::Relaxed);
                    let reply = match result {
                        Ok(reply) => CommandResult::SetEffort { status: ReplyStatus::from_wire(&reply), effort: wire_string(&reply, "effort"),
                            verification_pending: reply.get("verification_pending").and_then(Value::as_bool) },
                        Err(error) => CommandResult::SetEffort { status: ReplyStatus::failed(error.to_string()), effort: None, verification_pending: None },
                    };
                    if frames.send(command_frame(id, reply)).is_err() { return; }
                }
                Ok(WorkerCommand::SetIsolation(id, profile)) => {
                    let migration=doxa_isolation::Profile::parse(&profile).ok().filter(|target| {
                        client.hello["isolation"]["profile"].as_str().and_then(|profile|doxa_isolation::Profile::parse(profile).ok()).is_some_and(|current|current.docker()!=target.docker())
                    });
                    if id==session_id {
                        if let Some(target)=migration{
                            let reply=match crate::isolation_migration::change(&mut client,target){
                                Ok((resumed,isolation))=>{
                                    client=resumed;live_from_seq=client.hello["next_seq"].as_u64().unwrap_or(u64::MAX);
                                    cursor.store(client.cursor,Ordering::Relaxed);
                                    if frames.send(WorkerFrame::Daemon{session_id:session_id.clone(),frame:client.hello.clone()}).is_err(){return;}
                                    CommandResult::SetIsolation{status:ReplyStatus{ok:true,error:None},isolation:Some(isolation)}
                                }
                                Err(error)=>CommandResult::SetIsolation{status:ReplyStatus::failed(error.to_string()),isolation:None},
                            };
                            if frames.send(command_frame(id,reply)).is_err(){return;}
                            continue;
                        }
                    }
                    let result = if id == session_id {
                        let mut params = Map::new();
                        params.insert("profile".into(),Value::String(profile)); params.insert("confirmed".into(),Value::Bool(true));
                        client.call("set_isolation",params)
                    } else { Err(TransportError::Malformed("isolation target is not attached")) };
                    cursor.store(client.cursor,Ordering::Relaxed);
                    let reply=match result {
                        Ok(reply)=>CommandResult::SetIsolation{status:ReplyStatus::from_wire(&reply),isolation:wire_value(&reply,"isolation")},
                        Err(error)=>CommandResult::SetIsolation{status:ReplyStatus::failed(error.to_string()),isolation:None},
                    };
                    if frames.send(command_frame(id,reply)).is_err(){return;}
                }
                Ok(WorkerCommand::SetPermissionMode(id, mode)) => {
                    let result = if id == session_id {
                        let mut params = Map::new();
                        params.insert("mode".into(), Value::String(mode));
                        client.call("set_permission_mode", params)
                    } else { Err(TransportError::Malformed("model target is not attached")) };
                    cursor.store(client.cursor, Ordering::Relaxed);
                    let reply = match result {
                        Ok(reply) => CommandResult::SetPermissionMode { status: ReplyStatus::from_wire(&reply), mode: wire_string(&reply, "mode") },
                        Err(error) => CommandResult::SetPermissionMode { status: ReplyStatus::failed(error.to_string()), mode: None },
                    };
                    if frames.send(command_frame(id, reply)).is_err() { return; }
                }
                Ok(WorkerCommand::Branch(id, target)) => {
                    let result = if id == session_id {
                        let mut params = Map::new();
                        if let Some(name) = target.as_ref() { params.insert("name".into(), Value::String(name.clone())); }
                        client.call(if target.is_some() { "switch_branch" } else { "branch" }, params)
                    } else { Err(TransportError::Malformed("branch target is not attached")) };
                    cursor.store(client.cursor, Ordering::Relaxed);
                    let reply = match result {
                        Ok(reply) => CommandResult::Branch { status: ReplyStatus::from_wire(&reply), base: wire_value(&reply, "base"),
                            branches: wire_value(&reply, "branches"), message: wire_string(&reply, "message") },
                        Err(error) => CommandResult::Branch { status: ReplyStatus::failed(error.to_string()), base: None, branches: None, message: None },
                    };
                    if frames.send(command_frame(id, reply)).is_err() { return; }
                }
                Ok(WorkerCommand::ContextDetail(id)) => {
                    let result = if id == session_id { client.call("context_detail", Map::new()) }
                        else { Err(TransportError::Malformed("context target is not attached")) };
                    cursor.store(client.cursor, Ordering::Relaxed);
                    let frame = match &result {
                        Ok(reply) if reply["ok"] == true => CommandResult::ContextDetail { status: ReplyStatus { ok: true, error: None }, detail: wire_value(reply, "result") },
                        Ok(_) => CommandResult::ContextDetail { status: ReplyStatus::failed("Context detail is unavailable for this session"), detail: None },
                        Err(error) => CommandResult::ContextDetail { status: ReplyStatus::failed(error.to_string()), detail: None },
                    };
                    if frames.send(command_frame(id, frame)).is_err() { return; }
                    if matches!(result, Err(TransportError::Closed)) { return; }
                }
                Ok(WorkerCommand::QueueList(id)) => {
                    let result = if id == session_id { client.call("queue", Map::new()) }
                        else { Err(TransportError::Malformed("queue target is not attached")) };
                    cursor.store(client.cursor, Ordering::Relaxed);
                    if matches!(result, Err(TransportError::Closed)) {
                        if let Some(guard) = roster_guard { revoke(guard); }
                    }
                    let frame = match &result {
                        Ok(reply) if reply["ok"] == true => CommandResult::QueueList { status: ReplyStatus { ok: true, error: None }, rows: safe_queue_rows(reply) },
                        Ok(reply) => CommandResult::QueueList { status: ReplyStatus::failed(reply["error"].as_str().unwrap_or("Queue unavailable")), rows: Vec::new() },
                        Err(error) => CommandResult::QueueList { status: ReplyStatus::failed(error.to_string()), rows: Vec::new() },
                    };
                    if frames.send(command_frame(id, frame)).is_err() { return; }
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
                    let status = match &result { Ok(reply) => ReplyStatus::from_wire(reply), Err(error) => ReplyStatus::failed(error.to_string()) };
                    if frames.send(command_frame(id, CommandResult::QueueCancel { status, queue_id })).is_err() { return; }
                    if matches!(result, Err(TransportError::Closed)) { return; }
                }
                Ok(WorkerCommand::Answer(session, id, answer)) => {
                    if session != session_id || !answer.is_object() {
                        let _ = frames.send(command_frame(session, CommandResult::Answer { request_id: id, ok: false, uncertain: None, message: "Invalid answer target or payload".into() }));
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
                    let frame = match &result {
                        Ok(reply) => CommandResult::Answer { request_id: id, ok: reply["ok"] == true, uncertain: None,
                            message: reply["error"].as_str().unwrap_or("Request no longer pending").to_owned() },
                        Err(error) => CommandResult::Answer { request_id: id, ok: false,
                            uncertain: Some(!matches!(error, TransportError::FrameTooLarge | TransportError::RequestIdsExhausted)), message: error.to_string() },
                    };
                    if frames.send(command_frame(session, frame)).is_err() {
                        return;
                    }
                    if matches!(result, Err(TransportError::Closed)) {
                        return;
                    }
                }
                Ok(WorkerCommand::Peers(id)) => {
                    if id != session_id {
                        let _ = frames.send(command_frame(id, CommandResult::PeerRoster { status: ReplyStatus::failed("Peer target is not attached"), peers: None }));
                        continue;
                    }
                    let result = client.call("peers", Map::new());
                    cursor.store(client.cursor, Ordering::Relaxed);
                    if matches!(result, Err(TransportError::Closed)) {
                        if let Some(guard) = roster_guard { revoke(guard); }
                    }
                    let reply = match &result {
                        Ok(reply) => CommandResult::PeerRoster { status: ReplyStatus::from_wire(reply), peers: wire_value(reply, "peers") },
                        Err(error) => CommandResult::PeerRoster { status: ReplyStatus::failed(error.to_string()), peers: None },
                    };
                    if frames.send(command_frame(id.clone(), reply)).is_err() { return; }
                    if matches!(result, Err(TransportError::Closed)) { return; }
                    let mut params = Map::new();
                    params.insert("limit".into(), Value::from(50));
                    let history = client.call("peer_history", params);
                    cursor.store(client.cursor, Ordering::Relaxed);
                    if matches!(history, Err(TransportError::Closed)) {
                        if let Some(guard) = roster_guard { revoke(guard); }
                    }
                    let reply = match &history {
                        Ok(reply) => CommandResult::PeerHistory { status: ReplyStatus::from_wire(reply), messages: wire_value(reply, "messages") },
                        Err(error) => CommandResult::PeerHistory { status: ReplyStatus::failed(error.to_string()), messages: None },
                    };
                    if frames.send(command_frame(id, reply)).is_err() { return; }
                    if matches!(history, Err(TransportError::Closed)) { return; }
                }
                Ok(WorkerCommand::Message(id, target, text)) => {
                    let draft = format!("/msg {target} {text}");
                    if id != session_id {
                        let _ = frames.send(command_frame(id, CommandResult::PeerMessage { status: ReplyStatus::failed("Peer target session is not attached"), uncertain: None,
                            draft, peer: None, delivered_to: None, failed: None, ledger_error: None }));
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
                        Ok(reply) => CommandResult::PeerMessage { status: ReplyStatus::from_wire(reply), uncertain: None,
                            draft, peer: wire_value(reply, "peer"), delivered_to: wire_value(reply, "delivered_to"),
                            failed: wire_value(reply, "failed"), ledger_error: wire_value(reply, "ledger_error") },
                        Err(error) => CommandResult::PeerMessage { status: ReplyStatus::failed(error.to_string()),
                            uncertain: Some(!matches!(error, TransportError::FrameTooLarge | TransportError::RequestIdsExhausted)),
                            draft, peer: None, delivered_to: None, failed: None, ledger_error: None },
                    };
                    if frames.send(command_frame(id, reply)).is_err() { return; }
                    if matches!(result, Err(TransportError::Closed)) { return; }
                }
                Ok(WorkerCommand::Prompt(id, text)) => {
                    if id != session_id {
                        if frames
                            .send(WorkerFrame::PromptFailed { session_id: id, text, message: "Prompt target is not attached".into(), delivery: PromptDelivery::Rejected })
                            .is_err()
                        {
                            return;
                        }
                        continue;
                    }
                    match client.prompt(&text) {
                        Ok(reply) => {
                            let rejected = reply["ok"] == false;
                            let frame = if rejected {
                                WorkerFrame::PromptFailed { session_id: id, text, delivery: PromptDelivery::Rejected,
                                    message: reply["error"].as_str().unwrap_or("Prompt refused").to_owned() }
                            } else { WorkerFrame::Daemon { session_id: session_id.clone(), frame: reply } };
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
                            let (delivery, message) = match error {
                                TransportError::FrameTooLarge
                                | TransportError::RequestIdsExhausted => {
                                    (PromptDelivery::Rejected, "Prompt could not be sent")
                                }
                                TransportError::Timeout => {
                                    (PromptDelivery::Uncertain, "Prompt delivery unconfirmed")
                                }
                                TransportError::Closed => (
                                    PromptDelivery::Uncertain,
                                    "Daemon disconnected; prompt delivery unconfirmed",
                                ),
                                _ => (PromptDelivery::Uncertain, "Prompt delivery unconfirmed"),
                            };
                            let _ =
                                frames.send(WorkerFrame::PromptFailed { session_id: session_id.clone(), text, message: message.into(), delivery });
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
            Ok(Some(frame)) => {
                let terminal = frame["type"] == "event"
                    && matches!(frame["event"]["type"].as_str(), Some("turn_done" | "turn_refused"))
                    && frame["seq"].as_u64().is_some_and(|seq| seq >= live_from_seq);
                if frames.send(WorkerFrame::Daemon { session_id: session_id.clone(), frame }).is_err() {
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
                let _ = frames.send(WorkerFrame::Notice { session_id: session_id.clone(), message: message.into() });
                return;
            }
        }
        // The vendor host reads balance in a bounded background task. Poll
        // only its cached status so an idle session gains the chip too.
        if deepseek && balance_checked.elapsed() >= Duration::from_secs(5) {
            balance_checked = Instant::now();
            if !forward_status(&mut client, frames, &session_id) { return; }
        }
        cursor.store(client.cursor, Ordering::Relaxed);
    }
}

fn forward_status(client: &mut DaemonClient, frames: &SyncSender<WorkerFrame>, session_id: &str) -> bool {
    match client.call("status", Map::new()) {
        Ok(reply) if reply["ok"] == true && reply.get("status").is_some() => {
            // Do not restore activity from a possibly stale cached status.
            frames.send(WorkerFrame::Telemetry { session_id: session_id.to_owned(), reply }).is_ok()
        }
        Err(TransportError::Closed) => false,
        _ => frames.send(WorkerFrame::TelemetryUnavailable { session_id: session_id.to_owned() }).is_ok(),
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
    fn typed_router_rejections_preserve_each_original_command_owner() {
        let bridge = connect_sessions_inner(&[], true).unwrap();
        bridge.commands.send(WorkerCommand::SetEffort("background".into(), "low".into())).unwrap();
        bridge.commands.send(WorkerCommand::Answer("other".into(), "permission-7".into(), json!({"decision":"deny"}))).unwrap();
        bridge.commands.send(WorkerCommand::Message("draft-owner".into(), "peer-2".into(), "retain me".into())).unwrap();
        bridge.commands.send(WorkerCommand::QueueCancel("queue-owner".into(), "queue-4".into())).unwrap();
        let frame = bridge.frames.recv_timeout(Duration::from_secs(2)).unwrap();
        assert!(matches!(frame, WorkerFrame::Command { session_id, result: CommandResult::SetEffort { status, effort: None, verification_pending: None } }
            if session_id == "background" && !status.ok && status.error.as_deref() == Some("Session is not attached")));
        let frame = bridge.frames.recv_timeout(Duration::from_secs(2)).unwrap();
        assert!(matches!(frame, WorkerFrame::Command { session_id, result: CommandResult::Answer { request_id, ok: false, uncertain: None, .. } }
            if session_id == "other" && request_id == "permission-7"));
        let frame = bridge.frames.recv_timeout(Duration::from_secs(2)).unwrap();
        assert!(matches!(frame, WorkerFrame::Command { session_id, result: CommandResult::PeerMessage { status, draft, uncertain: Some(false), .. } }
            if session_id == "draft-owner" && !status.ok && draft == "/msg peer-2 retain me"));
        let frame = bridge.frames.recv_timeout(Duration::from_secs(2)).unwrap();
        assert!(matches!(frame, WorkerFrame::Command { session_id, result: CommandResult::QueueCancel { status, queue_id } }
            if session_id == "queue-owner" && !status.ok && queue_id == "queue-4"));
        bridge.shutdown();
    }

    #[test]
    fn typed_worker_refuses_malformed_control_fields_and_wrong_session_targets() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("daemon.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let server = thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            writeln!(socket, "{}", json!({"type":"hello","proto":1,"session_id":"owned","engine":"claude","cwd":"/","next_seq":0})).unwrap();
            let mut reader = BufReader::new(socket.try_clone().unwrap());
            let mut line = String::new(); reader.read_line(&mut line).unwrap(); // attach
            line.clear(); reader.read_line(&mut line).unwrap();
            let request: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(request["method"], "set_effort");
            writeln!(socket, "{}", json!({"type":"reply","id":request["id"],"ok":true,"effort":42,"verification_pending":"false","error":false,"session_id":"spoofed"})).unwrap();
            // No RPC is emitted for the following mismatched owner.
            line.clear(); assert_eq!(reader.read_line(&mut line).unwrap(), 0);
        });
        let (frames, commands, worker) = spawn_worker(DaemonClient::connect(&path, None).unwrap());
        assert!(matches!(frames.recv_timeout(Duration::from_secs(2)).unwrap(), WorkerFrame::Daemon { session_id, .. } if session_id == "owned"));
        commands.send(WorkerCommand::SetEffort("owned".into(), "low".into())).unwrap();
        assert!(matches!(frames.recv_timeout(Duration::from_secs(2)).unwrap(), WorkerFrame::Command { session_id,
            result: CommandResult::SetEffort { status: ReplyStatus { ok: true, error: None }, effort: None, verification_pending: None } } if session_id == "owned"));
        commands.send(WorkerCommand::SetEffort("different".into(), "high".into())).unwrap();
        assert!(matches!(frames.recv_timeout(Duration::from_secs(2)).unwrap(), WorkerFrame::Command { session_id,
            result: CommandResult::SetEffort { status, effort: None, verification_pending: None } } if session_id == "different" && !status.ok));
        drop(commands); worker.join().unwrap(); server.join().unwrap();
    }

    #[test]
    fn typed_router_command_backpressure_returns_the_unsent_command_without_losing_its_target() {
        let bridge = connect_sessions_inner(&[], true).unwrap();
        let mut rejected = None;
        // Exercise the real router's bounded command channel with its result
        // receiver undrained; a full queue must return the exact unsent draft.
        for index in 0..10_000 {
            let command = WorkerCommand::Prompt(format!("owner-{index}"), format!("draft-{index}"));
            match bridge.commands.try_send(command) {
                Ok(()) => {},
                Err(mpsc::TrySendError::Full(command)) => { rejected = Some((index, command)); break; },
                Err(mpsc::TrySendError::Disconnected(_)) => panic!("router ended during owned backpressure test"),
            }
        }
        let (index, command) = rejected.expect("bounded queues must reject the unsent command");
        assert!(matches!(command, WorkerCommand::Prompt(id, text) if id == format!("owner-{index}") && text == format!("draft-{index}")));
        // Shutdown must join even while there are undrained queued results.
        bridge.shutdown();
    }

    #[test]
    fn effort_verification_flag_survives_daemon_worker_bridge() {
        let path = std::env::temp_dir().join(format!("doxa-effort-{}-{}.sock", std::process::id(), NEXT_SOCKET.fetch_add(1, Ordering::Relaxed)));
        let listener = UnixListener::bind(&path).unwrap();
        let server = thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            writeln!(socket, "{}", json!({"type":"hello","proto":1,"session_id":"s","engine":"claude","cwd":"/","next_seq":1})).unwrap();
            let mut reader = BufReader::new(socket.try_clone().unwrap());
            let mut line = String::new(); reader.read_line(&mut line).unwrap(); // attach
            for pending in [true, false] {
                line.clear(); reader.read_line(&mut line).unwrap();
                let request: Value = serde_json::from_str(&line).unwrap();
                assert_eq!(request["method"], "set_effort");
                writeln!(socket, "{}", json!({"type":"reply","id":request["id"],"ok":true,"effort":"low","verification_pending":pending})).unwrap();
            }
        });
        let (frames, commands, worker) = spawn_worker(DaemonClient::connect(&path, None).unwrap());
        assert_eq!(frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value()["type"], "hello");
        for pending in [true, false] {
            commands.send(WorkerCommand::SetEffort("s".into(), "low".into())).unwrap();
            let reply = frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value();
            assert_eq!(reply["type"], "set_effort_reply"); assert_eq!(reply["session_id"], "s");
            assert_eq!(reply["verification_pending"], pending);
        }
        drop(commands); worker.join().unwrap(); server.join().unwrap(); std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn initial_roster_accepts_256_and_all_failed_notices_cannot_block_mount() {
        let dir=tempfile::tempdir().unwrap();
        let rows:Vec<_>=(0..256).map(|i|Session{id:format!("saved-{i}"),title:String::new(),
            socket:dir.path().join(format!("missing-{i}")),scope_key:String::new(),clients:None,started_at:String::new()}).collect();
        assert_eq!(connect_sessions(&rows).err().unwrap().kind(),io::ErrorKind::NotConnected);
        let mut too_many=rows;too_many.push(Session{id:"extra".into(),title:String::new(),socket:dir.path().join("missing"),scope_key:String::new(),clients:None,started_at:String::new()});
        assert_eq!(connect_sessions(&too_many).err().unwrap().kind(),io::ErrorKind::NotConnected);
        too_many.push(Session{id:"extra-2".into(),title:String::new(),socket:dir.path().join("missing-2"),scope_key:String::new(),clients:None,started_at:String::new()});
        assert_eq!(connect_sessions(&too_many).err().unwrap().kind(),io::ErrorKind::InvalidInput);
        connect_sessions_inner(&[],true).unwrap().shutdown();
    }

    #[test]
    fn intentional_clear_finalization_keeps_complete_roster_gate() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("daemon.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let server = thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            writeln!(socket, "{}", json!({"type":"hello", "proto":1,
                "session_id":"old", "engine":"codex", "cwd":"/repo", "next_seq":0})).unwrap();
            let mut reader = BufReader::new(socket.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap(); // attach
            line.clear();
            reader.read_line(&mut line).unwrap();
            let call: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(call["method"], "stop_if_idle");
            writeln!(socket, "{}", json!({"type":"reply", "id":call["id"], "ok":true})).unwrap();
        });
        let session = Session { id:"old".into(), title:"gpt-6-sol@main/doxa".into(), socket:path,
            scope_key:String::new(), clients:None, started_at:String::new() };
        let (frame_tx, frame_rx) = mpsc::sync_channel(16);
        let complete = Arc::new(Mutex::new(true));
        let (commands, _, worker) = attach_worker(&session, &frame_tx, &complete).unwrap();
        let hello = frame_rx.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value();
        assert_eq!(hello["type"], "hello");
        assert_eq!(hello["title"], "gpt-6-sol@main/doxa");
        commands.send(WorkerCommand::FinalizeForClear("old".into())).unwrap();
        let reply = frame_rx.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value();
        assert_eq!(reply["type"], "clear_finalize_reply");
        assert_eq!(reply["ok"], true);
        worker.join().unwrap();
        server.join().unwrap();
        assert!(*complete.lock().unwrap());
    }

    #[test]
    fn fresh_socket_attach_uses_registry_title_for_matching_identity_and_socket() {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("daemon.sock");
        let alias = dir.path().join("alias.sock");
        let _listener = UnixListener::bind(&socket).unwrap();
        std::os::unix::fs::symlink(&socket, &alias).unwrap();
        let rows = vec![
            Session { id:"new".into(), title:"wrong".into(), socket:"/other.sock".into(),
                scope_key:String::new(), clients:None, started_at:String::new() },
            Session { id:"new".into(), title:"opus-5-5@main/doxa".into(), socket:socket.clone(),
                scope_key:String::new(), clients:None, started_at:String::new() },
        ];
        assert_eq!(registered_socket(rows.clone(), "new", &fs::canonicalize(alias).unwrap()).unwrap().title,
            "opus-5-5@main/doxa");
        assert!(registered_socket(rows, "new", Path::new("/missing.sock")).is_err());
    }

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
        let rows = safe_queue_rows_with_client(&json!({"queue":[
            {"id":"q7","text":"my SECRET token"}, {"id":"../unsafe","text":"SECRET"}
        ]}), doxa_lore::LoreClient::spawn(&script, Duration::from_secs(2)).ok());
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].id, "q7");
        assert_eq!(rows[0].preview, "my [redacted] token");
        assert!(!serde_json::to_string(&rows).unwrap().contains("SECRET"));
        let unavailable = safe_queue_rows_with_client(&json!({"queue":[{"id":"q8","text":"SECRET"}]}),
            None);
        assert_eq!(unavailable[0].preview, "[preview unavailable]");
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
                json!({"type":"call","id":3,"method":"peer_history","params":{"limit":50}}));
            writeln!(socket, "{}", json!({"type":"reply","id":3,"ok":true,"messages":[]})).unwrap();
            line.clear();
            reader.read_line(&mut line).unwrap();
            assert_eq!(serde_json::from_str::<Value>(&line).unwrap(),
                json!({"type":"call","id":4,"method":"msg",
                    "params":{"target":"peer-1","text":"hello"}}));
            writeln!(socket, "{}", json!({"type":"reply","id":4,"ok":true,
                "peer":{"session_id":"peer-1","title":"Builder"},
                "delivered_to":["peer-1"],"failed":[]})).unwrap();
        });
        let client = DaemonClient::connect(&path, None).unwrap();
        let (frames, prompts, worker) = spawn_worker(client);
        assert_eq!(
            frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value()["session_id"],
            "session-1"
        );
        let event = frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value();
        assert_eq!(event["session_id"], "session-1");
        assert_eq!(event["event"]["data"]["text"], "hello");
        prompts
            .send(WorkerCommand::Prompt("session-1".into(), "next".into()))
            .unwrap();
        assert_eq!(
            frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value()["turn"],
            "turn-1"
        );
        prompts.send(WorkerCommand::Peers("session-1".into())).unwrap();
        let peers = frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value();
        assert_eq!(peers["type"], "peer_roster");
        assert_eq!(peers["peers"][0]["session_id"], "peer-1");
        let history = frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value();
        assert_eq!(history["type"], "peer_history");
        assert_eq!(history["messages"], json!([]));
        prompts.send(WorkerCommand::Message("session-1".into(), "peer-1".into(), "hello".into())).unwrap();
        let sent = frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value();
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
        assert_eq!(frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value()["lore_scrub"], "ready");
        assert_eq!(frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value()["event"]["type"], "turn_done");
        let status = frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value();
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
        frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value(); // hello
        prompts
            .send(WorkerCommand::Prompt(
                "session-1".into(),
                "please retry".into(),
            ))
            .unwrap();
        let rejected = frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value();
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
        frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value();
        commands
            .send(WorkerCommand::Answer(
                "session-1".into(),
                "req-1".into(),
                json!({"decision":"deny"}),
            ))
            .unwrap();
        let reply = frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value();
        assert_eq!(reply["type"], "answer_reply");
        assert_eq!(reply["ok"], true);
        drop(commands);
        worker.join().unwrap();
        server.join().unwrap();
        std::fs::remove_file(path).unwrap();
    }
}
